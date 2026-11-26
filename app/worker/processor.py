"""Per-message investigation logic (F07), kept separate from the polling loop.

Safe under at-least-once delivery:
- `claim` returns None for a missing/completed/failed case, so a duplicate is just acked.
- The graph is resumed from its checkpoint (thread id `alert-{alert_id}`), so a worker
  that died mid-run continues instead of starting over.
- `complete` is one idempotent UPDATE; the SQS message is deleted only after it.

F08: an escalated case stops at the `human_review` interrupt. The worker then marks the
case `awaiting_review` and acks. After the analyst decides, the API sends a `resume`
message and the graph continues with `Command(resume=decision)` from its checkpoint.
"""

import asyncio
import contextlib
import logging
from collections.abc import Callable
from typing import Any, Literal

from langgraph.types import Command

from app.agent.gateway import case_scope
from app.db.cases import CaseStore
from app.messaging.sqs import InvestigationQueue, QueueMessage
from app.services.events import EventPublisher
from app.services.locks import RedisLock, case_lock_key

log = logging.getLogger("worker")

Result = Literal["completed", "awaiting_review", "skipped", "retry", "failed"]


def thread_config(alert_id: int) -> dict[str, Any]:
    return {"configurable": {"thread_id": f"alert-{alert_id}"}}


def pending_interrupts(snapshot: Any) -> list[Any]:
    """Interrupts the graph is waiting on (empty for a crash, which also has `next`)."""
    return [i for t in getattr(snapshot, "tasks", ()) for i in t.interrupts]


def is_paused(state: dict[str, Any]) -> bool:
    return bool(state.get("__interrupt__"))


Publish = Callable[[str, dict[str, Any]], Any]


async def _drive(
    graph: Any, graph_input: Any, config: dict[str, Any], publish: Publish | None
) -> dict[str, Any]:
    """Run the graph; with `publish`, stream node updates (F10) so progress is visible."""
    if publish is None:
        return await graph.ainvoke(graph_input, config)  # type: ignore[no-any-return]
    async for chunk in graph.astream(graph_input, config, stream_mode="updates"):
        for node in chunk:
            if node != "__interrupt__":
                await publish("node_finished", {"node": node})
    snapshot = await graph.aget_state(config)
    state = dict(snapshot.values)
    if pending := pending_interrupts(snapshot):
        state["__interrupt__"] = pending
    return state


async def run_graph(
    graph: Any, alert_id: int, publish: Publish | None = None
) -> dict[str, Any]:
    """Start the investigation, or continue it from its checkpoint."""
    config = thread_config(alert_id)
    snapshot = await graph.aget_state(config)
    if pending := pending_interrupts(snapshot):  # already waiting for the analyst
        log.info("alert %s: already waiting for analyst review", alert_id)
        return {**snapshot.values, "__interrupt__": pending}
    if snapshot.next:  # interrupted by a crash or error: continue where it stopped
        log.info(
            "alert %s: resuming from checkpoint (next=%s)", alert_id, snapshot.next
        )
        return await _drive(graph, None, config, publish)
    if snapshot.values:  # graph finished earlier; only the DB write was lost
        log.info("alert %s: graph already finished, reusing final state", alert_id)
        return dict(snapshot.values)
    return await _drive(graph, {"alert_id": alert_id}, config, publish)


async def resume_graph(
    graph: Any,
    alert_id: int,
    decision: dict[str, Any],
    publish: Publish | None = None,
) -> dict[str, Any]:
    """Continue a paused graph with the analyst's decision (safe to repeat)."""
    config = thread_config(alert_id)
    snapshot = await graph.aget_state(config)
    if pending_interrupts(snapshot):
        return await _drive(graph, Command(resume=decision), config, publish)
    if snapshot.next:  # resumed earlier, then crashed before finishing
        return await _drive(graph, None, config, publish)
    if snapshot.values:  # finished earlier; only the DB write was lost
        return dict(snapshot.values)
    raise RuntimeError(f"alert {alert_id}: no checkpoint to resume")


class Processor:
    def __init__(
        self,
        graph: Any,
        store: CaseStore,
        queue: InvestigationQueue,
        max_receive_count: int,
        visibility_timeout: int,
        redis: Any = None,
        events: EventPublisher | None = None,
        lock_ttl: int = 300,
    ) -> None:
        self._redis = redis
        self._events = events
        self._lock_ttl = lock_ttl
        self._graph = graph
        self._store = store
        self._queue = queue
        self._max_receive = max_receive_count
        self._visibility = visibility_timeout

    def _publisher(self, case_id: int) -> Publish | None:
        events = self._events
        if events is None:
            return None

        async def publish(type_: str, data: dict[str, Any]) -> None:
            await events.publish(case_id, type_, data)

        return publish

    async def _heartbeat(
        self, receipt: str, case_id: int, lock: RedisLock | None = None
    ) -> None:
        """While a run is in progress: keep the message invisible and the case 'fresh'."""
        while True:
            await asyncio.sleep(self._visibility / 2)
            await self._store.touch(case_id)
            if lock is not None:
                await lock.renew()
            await self._queue.change_visibility(receipt, self._visibility)

    async def handle(self, msg: QueueMessage) -> Result:
        resume = msg.kind == "resume"
        # A running case untouched for 3/4 of the visibility timeout has a dead worker.
        stale = int(self._visibility * 0.75)
        case = await (
            self._store.claim_resume(msg.case_id, stale)
            if resume
            else self._store.claim(msg.case_id, stale)
        )
        if case is None:
            log.info("case %s: nothing to do, acknowledging", msg.case_id)
            await self._queue.delete(msg.receipt_handle)
            return "skipped"
        lock: RedisLock | None = None
        if self._redis is not None:
            lock = RedisLock(self._redis, case_lock_key(msg.case_id), self._lock_ttl)
            if not await lock.acquire():
                # Another worker is running this case; leave the message for redelivery.
                log.info("case %s: locked by another worker", msg.case_id)
                return "retry"
        publish = self._publisher(msg.case_id)
        beat = asyncio.create_task(
            self._heartbeat(msg.receipt_handle, msg.case_id, lock)
        )
        result: Result
        try:
            if publish:
                await publish("started", {"resume": resume})
            with case_scope(msg.case_id):  # F12: attributes LLM usage to this case
                if resume:
                    state = await self._resume(msg, publish)
                else:
                    state = await run_graph(self._graph, msg.alert_id, publish)
            if is_paused(state):
                await self._store.mark_awaiting_review(msg.case_id, state)
                result = "awaiting_review"
            else:
                await self._store.complete(msg.case_id, state)
                result = "completed"
            if publish:
                await publish("status", {"status": result})
        except Exception as exc:
            final = msg.receive_count >= self._max_receive
            log.exception("case %s failed (receive %s)", msg.case_id, msg.receive_count)
            await self._store.record_error(
                msg.case_id,
                repr(exc),
                failed=final,
                retry_status="awaiting_review" if resume else "queued",
            )
            if publish:
                await publish("status", {"status": "failed" if final else "retrying"})
            # Not deleted: SQS redelivers, and moves it to the DLQ after the last try.
            return "failed" if final else "retry"
        finally:
            if lock is not None:
                await lock.release()
            beat.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await beat
        await self._queue.delete(msg.receipt_handle)
        return result

    async def _resume(
        self, msg: QueueMessage, publish: Publish | None = None
    ) -> dict[str, Any]:
        decision = await self._store.get_decision(msg.case_id)
        if decision is None:
            raise RuntimeError(f"case {msg.case_id}: resume without a decision")
        payload = {
            "decision": decision["decision"],
            "analyst_id": decision["analyst_id"],
            "notes": decision["notes"],
        }
        return await resume_graph(self._graph, msg.alert_id, payload, publish)

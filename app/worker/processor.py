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
from typing import Any, Literal

from langgraph.types import Command

from app.db.cases import CaseStore
from app.messaging.sqs import InvestigationQueue, QueueMessage

log = logging.getLogger("worker")

Result = Literal["completed", "awaiting_review", "skipped", "retry", "failed"]


def thread_config(alert_id: int) -> dict[str, Any]:
    return {"configurable": {"thread_id": f"alert-{alert_id}"}}


def pending_interrupts(snapshot: Any) -> list[Any]:
    """Interrupts the graph is waiting on (empty for a crash, which also has `next`)."""
    return [i for t in getattr(snapshot, "tasks", ()) for i in t.interrupts]


def is_paused(state: dict[str, Any]) -> bool:
    return bool(state.get("__interrupt__"))


async def run_graph(graph: Any, alert_id: int) -> dict[str, Any]:
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
        return await graph.ainvoke(None, config)  # type: ignore[no-any-return]
    if snapshot.values:  # graph finished earlier; only the DB write was lost
        log.info("alert %s: graph already finished, reusing final state", alert_id)
        return dict(snapshot.values)
    return await graph.ainvoke({"alert_id": alert_id}, config)  # type: ignore[no-any-return]


async def resume_graph(
    graph: Any, alert_id: int, decision: dict[str, Any]
) -> dict[str, Any]:
    """Continue a paused graph with the analyst's decision (safe to repeat)."""
    config = thread_config(alert_id)
    snapshot = await graph.aget_state(config)
    if pending_interrupts(snapshot):
        return await graph.ainvoke(Command(resume=decision), config)  # type: ignore[no-any-return]
    if snapshot.next:  # resumed earlier, then crashed before finishing
        return await graph.ainvoke(None, config)  # type: ignore[no-any-return]
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
    ) -> None:
        self._graph = graph
        self._store = store
        self._queue = queue
        self._max_receive = max_receive_count
        self._visibility = visibility_timeout

    async def _heartbeat(self, receipt: str, case_id: int) -> None:
        """While a run is in progress: keep the message invisible and the case 'fresh'."""
        while True:
            await asyncio.sleep(self._visibility / 2)
            await self._store.touch(case_id)
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
        beat = asyncio.create_task(self._heartbeat(msg.receipt_handle, msg.case_id))
        result: Result
        try:
            if resume:
                state = await self._resume(msg)
            else:
                state = await run_graph(self._graph, msg.alert_id)
            if is_paused(state):
                await self._store.mark_awaiting_review(msg.case_id, state)
                result = "awaiting_review"
            else:
                await self._store.complete(msg.case_id, state)
                result = "completed"
        except Exception as exc:
            final = msg.receive_count >= self._max_receive
            log.exception("case %s failed (receive %s)", msg.case_id, msg.receive_count)
            await self._store.record_error(
                msg.case_id,
                repr(exc),
                failed=final,
                retry_status="awaiting_review" if resume else "queued",
            )
            # Not deleted: SQS redelivers, and moves it to the DLQ after the last try.
            return "failed" if final else "retry"
        finally:
            beat.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await beat
        await self._queue.delete(msg.receipt_handle)
        return result

    async def _resume(self, msg: QueueMessage) -> dict[str, Any]:
        decision = await self._store.get_decision(msg.case_id)
        if decision is None:
            raise RuntimeError(f"case {msg.case_id}: resume without a decision")
        payload = {
            "decision": decision["decision"],
            "analyst_id": decision["analyst_id"],
            "notes": decision["notes"],
        }
        return await resume_graph(self._graph, msg.alert_id, payload)

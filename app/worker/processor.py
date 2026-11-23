"""Per-message investigation logic (F07), kept separate from the polling loop.

Safe under at-least-once delivery:
- `claim` returns None for a missing/completed/failed case, so a duplicate is just acked.
- The graph is resumed from its checkpoint (thread id `alert-{alert_id}`), so a worker
  that died mid-run continues instead of starting over.
- `complete` is one idempotent UPDATE; the SQS message is deleted only after it.
"""

import asyncio
import contextlib
import logging
from typing import Any, Literal

from app.db.cases import CaseStore
from app.messaging.sqs import InvestigationQueue, QueueMessage

log = logging.getLogger("worker")

Result = Literal["completed", "skipped", "retry", "failed"]


def thread_config(alert_id: int) -> dict[str, Any]:
    return {"configurable": {"thread_id": f"alert-{alert_id}"}}


async def run_graph(graph: Any, alert_id: int) -> dict[str, Any]:
    """Start the investigation, or continue it from its checkpoint."""
    config = thread_config(alert_id)
    snapshot = await graph.aget_state(config)
    if snapshot.next:  # interrupted by a crash or error: continue where it stopped
        log.info(
            "alert %s: resuming from checkpoint (next=%s)", alert_id, snapshot.next
        )
        return await graph.ainvoke(None, config)  # type: ignore[no-any-return]
    if snapshot.values:  # graph finished earlier; only the DB write was lost
        log.info("alert %s: graph already finished, reusing final state", alert_id)
        return dict(snapshot.values)
    return await graph.ainvoke({"alert_id": alert_id}, config)  # type: ignore[no-any-return]


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
        # A running case untouched for 3/4 of the visibility timeout has a dead worker.
        case = await self._store.claim(msg.case_id, int(self._visibility * 0.75))
        if case is None:
            log.info("case %s: nothing to do, acknowledging", msg.case_id)
            await self._queue.delete(msg.receipt_handle)
            return "skipped"
        beat = asyncio.create_task(self._heartbeat(msg.receipt_handle, msg.case_id))
        try:
            state = await run_graph(self._graph, msg.alert_id)
            await self._store.complete(msg.case_id, state)
        except Exception as exc:
            final = msg.receive_count >= self._max_receive
            log.exception("case %s failed (receive %s)", msg.case_id, msg.receive_count)
            await self._store.record_error(msg.case_id, repr(exc), failed=final)
            # Not deleted: SQS redelivers, and moves it to the DLQ after the last try.
            return "failed" if final else "retry"
        finally:
            beat.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await beat
        await self._queue.delete(msg.receipt_handle)
        return "completed"

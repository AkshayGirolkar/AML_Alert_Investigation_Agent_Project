"""F07 unit tests: worker claim / resume / retry logic with fakes (no DB, no SQS)."""

from types import SimpleNamespace
from typing import Any

import pytest

from app.messaging.sqs import QueueMessage
from app.worker.processor import Processor, run_graph


class FakeStore:
    def __init__(self, status: str = "queued") -> None:
        self.case: dict[str, Any] | None = {
            "case_id": 1,
            "alert_id": 9,
            "status": status,
        }
        self.completed: list[dict[str, Any]] = []
        self.errors: list[tuple[str, bool]] = []

    async def claim(self, case_id: int, stale_after: int) -> dict[str, Any] | None:
        if self.case and self.case["status"] == "queued":
            self.case["status"] = "running"
            return self.case
        return None

    async def touch(self, case_id: int) -> None:
        pass

    async def complete(self, case_id: int, state: dict[str, Any]) -> None:
        self.completed.append(state)
        assert self.case is not None
        self.case["status"] = "completed"

    async def record_error(self, case_id: int, error: str, failed: bool) -> None:
        self.errors.append((error, failed))


class FakeQueue:
    def __init__(self) -> None:
        self.deleted: list[str] = []
        self.visibility: list[int] = []

    async def delete(self, receipt_handle: str) -> None:
        self.deleted.append(receipt_handle)

    async def change_visibility(self, receipt_handle: str, seconds: int) -> None:
        self.visibility.append(seconds)


class FakeGraph:
    def __init__(
        self,
        snapshot: SimpleNamespace | None = None,
        result: dict[str, Any] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.snapshot = snapshot or SimpleNamespace(next=(), values={})
        self.result = result or {
            "outcome": "closed",
            "verdict": {"disposition": "close"},
        }
        self.error = error
        self.invocations: list[Any] = []

    async def aget_state(self, config: dict[str, Any]) -> SimpleNamespace:
        return self.snapshot

    async def ainvoke(self, payload: Any, config: dict[str, Any]) -> dict[str, Any]:
        self.invocations.append((payload, config["configurable"]["thread_id"]))
        if self.error:
            raise self.error
        return self.result


def _msg(receive_count: int = 1) -> QueueMessage:
    return QueueMessage("rh", case_id=1, alert_id=9, receive_count=receive_count)


def _proc(graph: FakeGraph, store: FakeStore, queue: FakeQueue) -> Processor:
    return Processor(graph, store, queue, max_receive_count=3, visibility_timeout=300)  # type: ignore[arg-type]


async def test_new_case_runs_graph_stores_result_and_acks() -> None:
    graph, store, queue = FakeGraph(), FakeStore(), FakeQueue()
    assert await _proc(graph, store, queue).handle(_msg()) == "completed"
    assert graph.invocations == [({"alert_id": 9}, "alert-9")]
    assert store.completed[0]["outcome"] == "closed"
    assert queue.deleted == ["rh"]


@pytest.mark.parametrize("status", ["completed", "failed", "running"])
async def test_duplicate_delivery_is_acked_without_running_the_graph(
    status: str,
) -> None:
    graph, store, queue = FakeGraph(), FakeStore(status), FakeQueue()
    assert await _proc(graph, store, queue).handle(_msg()) == "skipped"
    assert graph.invocations == [] and store.completed == []
    assert queue.deleted == ["rh"]


async def test_missing_case_is_acked() -> None:
    store = FakeStore()
    store.case = None
    queue = FakeQueue()
    assert await _proc(FakeGraph(), store, queue).handle(_msg()) == "skipped"
    assert queue.deleted == ["rh"]


async def test_checkpoint_with_pending_nodes_is_resumed_not_restarted() -> None:
    snap = SimpleNamespace(next=("assess",), values={"alert_id": 9})
    graph = FakeGraph(snapshot=snap)
    await run_graph(graph, 9)
    assert graph.invocations == [(None, "alert-9")]  # None = continue the thread


async def test_finished_checkpoint_reuses_final_state_without_calling_the_llm() -> None:
    final = {"outcome": "escalated", "alert_id": 9}
    graph = FakeGraph(snapshot=SimpleNamespace(next=(), values=final))
    assert await run_graph(graph, 9) == final
    assert graph.invocations == []


async def test_failure_leaves_message_for_redelivery() -> None:
    graph = FakeGraph(error=RuntimeError("llm down"))
    store, queue = FakeStore(), FakeQueue()
    assert await _proc(graph, store, queue).handle(_msg(receive_count=1)) == "retry"
    assert queue.deleted == []  # not acked, so SQS redelivers
    assert store.errors and store.errors[0][1] is False
    assert "llm down" in store.errors[0][0]


async def test_failure_on_last_receive_marks_case_failed() -> None:
    graph = FakeGraph(error=RuntimeError("boom"))
    store, queue = FakeStore(), FakeQueue()
    assert await _proc(graph, store, queue).handle(_msg(receive_count=3)) == "failed"
    assert store.errors[0][1] is True and queue.deleted == []

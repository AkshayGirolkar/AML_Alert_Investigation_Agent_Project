"""F10.5 unit tests: the shared case commands, with a fake store and queue."""

from typing import Any

import pytest

from app.services import case_commands as cmd
from tests.unit.test_api import FakeQueue, FakeStore

pytestmark = pytest.mark.asyncio


async def test_start_unknown_alert_is_not_found_and_sends_nothing() -> None:
    store, queue = FakeStore({1}), FakeQueue()
    out = await cmd.start_investigation(store, queue, 9)  # type: ignore[arg-type]
    assert out.code == "not_found" and queue.sent == []


async def test_start_twice_creates_one_case() -> None:
    store, queue = FakeStore({1}), FakeQueue()
    a = await cmd.start_investigation(store, queue, 1)  # type: ignore[arg-type]
    b = await cmd.start_investigation(store, queue, 1)  # type: ignore[arg-type]
    assert a.code == b.code == "accepted" and len(store.cases) == 1


async def test_start_completed_case_is_not_enqueued() -> None:
    store, queue = FakeStore({1}), FakeQueue()
    await cmd.start_investigation(store, queue, 1)  # type: ignore[arg-type]
    store.cases[1]["status"] = "completed"
    out = await cmd.start_investigation(store, queue, 1)  # type: ignore[arg-type]
    assert out.code == "already_completed" and len(queue.sent) == 1


async def test_decision_on_waiting_case_sends_resume() -> None:
    store, queue = FakeStore({1}), FakeQueue()
    await cmd.start_investigation(store, queue, 1)  # type: ignore[arg-type]
    store.cases[1]["status"] = "awaiting_review"
    out = await cmd.record_decision(store, queue, 101, "approve", "a1", None)  # type: ignore[arg-type]
    assert out.result == "recorded" and queue.resumes == [(101, 1)]


async def test_decision_on_running_case_is_not_reviewable() -> None:
    store, queue = FakeStore({1}), FakeQueue()
    await cmd.start_investigation(store, queue, 1)  # type: ignore[arg-type]
    out = await cmd.record_decision(store, queue, 101, "approve", "a1", None)  # type: ignore[arg-type]
    assert out.result == "not_reviewable" and queue.resumes == []


async def test_audit_extra_is_forwarded_only_when_given() -> None:
    seen: list[Any] = []

    class Spy(FakeStore):
        async def record_decision(self, *args: Any, **kwargs: Any) -> Any:
            seen.append(kwargs)
            return "not_found", None, None

    spy, queue = Spy(set()), FakeQueue()
    await cmd.record_decision(spy, queue, 1, "approve", "a", None)  # type: ignore[arg-type]
    await cmd.record_decision(spy, queue, 1, "approve", "a", None, {"via": "console"})  # type: ignore[arg-type]
    assert seen == [{}, {"audit_extra": {"via": "console"}}]

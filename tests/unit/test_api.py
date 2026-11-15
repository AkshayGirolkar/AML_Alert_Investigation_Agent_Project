"""F07 unit tests: API handlers with a fake store and queue (no DB, no SQS)."""

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from app.main import create_app


class FakeStore:
    def __init__(self, alerts: set[int]) -> None:
        self.alerts = alerts
        self.cases: dict[int, dict[str, Any]] = {}

    async def alert_exists(self, alert_id: int) -> bool:
        return alert_id in self.alerts

    async def create_or_get(self, alert_id: int) -> tuple[dict[str, Any], bool]:
        if alert_id in self.cases:
            return self.cases[alert_id], False
        self.cases[alert_id] = {
            "case_id": alert_id + 100,
            "alert_id": alert_id,
            "status": "queued",
        }
        return self.cases[alert_id], True

    async def get(self, case_id: int) -> dict[str, Any] | None:
        return next((c for c in self.cases.values() if c["case_id"] == case_id), None)

    async def get_by_alert(self, alert_id: int) -> dict[str, Any] | None:
        return self.cases.get(alert_id)

    async def requeue(self, case_id: int) -> dict[str, Any] | None:
        case = await self.get(case_id)
        if case and case["status"] == "failed":
            case["status"] = "queued"
            return case
        return None


class FakeQueue:
    def __init__(self) -> None:
        self.sent: list[tuple[int, int]] = []

    async def send(self, case_id: int, alert_id: int) -> None:
        self.sent.append((case_id, alert_id))


@pytest.fixture
def parts() -> tuple[FakeStore, FakeQueue]:
    return FakeStore({1, 2}), FakeQueue()


@pytest.fixture
async def client(
    parts: tuple[FakeStore, FakeQueue],
) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(store=parts[0], queue=parts[1])  # type: ignore[arg-type]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as c:
        yield c


async def test_health(client: httpx.AsyncClient) -> None:
    r = await client.get("/health")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


async def test_start_enqueues_once_per_new_case(
    client: httpx.AsyncClient, parts: tuple[FakeStore, FakeQueue]
) -> None:
    r = await client.post("/alerts/1/investigations")
    assert r.status_code == 202 and r.json()["status"] == "queued"
    assert parts[1].sent == [(101, 1)]
    assert len(parts[0].cases) == 1


async def test_repeat_post_creates_no_second_case_and_resends_while_queued(
    client: httpx.AsyncClient, parts: tuple[FakeStore, FakeQueue]
) -> None:
    await client.post("/alerts/1/investigations")
    await client.post("/alerts/1/investigations")
    assert len(parts[0].cases) == 1
    assert parts[1].sent == [(101, 1), (101, 1)]  # still queued: re-send is harmless


async def test_running_case_is_not_enqueued_again(
    client: httpx.AsyncClient, parts: tuple[FakeStore, FakeQueue]
) -> None:
    await client.post("/alerts/1/investigations")
    parts[0].cases[1]["status"] = "running"
    r = await client.post("/alerts/1/investigations")
    assert r.status_code == 202 and len(parts[1].sent) == 1


async def test_completed_case_returns_200_and_does_not_enqueue(
    client: httpx.AsyncClient, parts: tuple[FakeStore, FakeQueue]
) -> None:
    await client.post("/alerts/1/investigations")
    parts[0].cases[1].update(status="completed", outcome="closed")
    r = await client.post("/alerts/1/investigations")
    assert r.status_code == 200 and r.json()["outcome"] == "closed"
    assert len(parts[1].sent) == 1


async def test_failed_case_is_requeued(
    client: httpx.AsyncClient, parts: tuple[FakeStore, FakeQueue]
) -> None:
    await client.post("/alerts/1/investigations")
    parts[0].cases[1]["status"] = "failed"
    r = await client.post("/alerts/1/investigations")
    assert r.json()["status"] == "queued" and len(parts[1].sent) == 2


async def test_unknown_alert_is_404_and_nothing_is_enqueued(
    client: httpx.AsyncClient, parts: tuple[FakeStore, FakeQueue]
) -> None:
    r = await client.post("/alerts/99/investigations")
    assert r.status_code == 404 and parts[1].sent == []


async def test_get_case_by_id_and_by_alert(client: httpx.AsyncClient) -> None:
    await client.post("/alerts/2/investigations")
    assert (await client.get("/cases/102")).json()["alert_id"] == 2
    assert (await client.get("/alerts/2/case")).json()["case_id"] == 102
    assert (await client.get("/cases/5")).status_code == 404
    assert (await client.get("/alerts/1/case")).status_code == 404

"""Smoke-test flow with a mock API (no network)."""

import httpx

from app.aws_smoke import run_smoke, wait_for_status


def _api(statuses: list[str], fail_decision: bool = False) -> httpx.MockTransport:
    seq = iter(statuses)
    last = {"s": statuses[0]}

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/ready":
            return httpx.Response(200, json={"status": "ok"})
        if path.endswith("/investigations"):
            return httpx.Response(202, json={"case_id": 7, "status": "queued"})
        if path == "/cases/7/decision":
            return httpx.Response(500 if fail_decision else 200, json={})
        if path == "/cases/7":
            last["s"] = next(seq, last["s"])
            return httpx.Response(200, json={"case_id": 7, "status": last["s"]})
        return httpx.Response(404)

    return httpx.MockTransport(handler)


async def _run(transport: httpx.MockTransport) -> object:
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        return await run_smoke(c, 1, timeout=5, interval=0)


async def test_escalated_case_is_approved_and_resumes() -> None:
    res = await _run(_api(["running", "awaiting_review", "running", "completed"]))
    assert res.ok and "decision approve accepted" in res.steps  # type: ignore[attr-defined]


async def test_closed_case_needs_no_decision() -> None:
    res = await _run(_api(["running", "completed"]))
    assert res.ok and not any("decision" in s for s in res.steps)  # type: ignore[attr-defined]


async def test_failed_case_is_not_ok() -> None:
    res = await _run(_api(["running", "failed"]))
    assert not res.ok and "failed" in (res.error or "")  # type: ignore[attr-defined]


async def test_decision_error_is_reported() -> None:
    res = await _run(_api(["awaiting_review"], fail_decision=True))
    assert not res.ok and res.error  # type: ignore[attr-defined]


async def test_wait_for_status_times_out() -> None:
    t = {"now": 0.0}

    async def get_case() -> dict[str, object]:
        return {"case_id": 1, "status": "running"}

    async def sleep(s: float) -> None:
        t["now"] += s

    import pytest

    with pytest.raises(TimeoutError):
        await wait_for_status(
            get_case, {"completed"}, 3, 1, clock=lambda: t["now"], sleep=sleep
        )


async def test_decision_omits_analyst_id_when_none() -> None:
    """With bearer auth the API takes the analyst from the token (F11)."""
    bodies: list[bytes] = []
    inner = _api(["awaiting_review", "running", "completed"])

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/decision"):
            bodies.append(req.content)
        return inner.handler(req)  # type: ignore[return-value]

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://t"
    ) as c:
        res = await run_smoke(c, 1, analyst_id=None, timeout=5, interval=0)
    assert res.ok and b"analyst_id" not in bodies[0]

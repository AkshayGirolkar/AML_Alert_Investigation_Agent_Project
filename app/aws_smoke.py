"""End-to-end smoke test against a deployed stack (F17): alert -> case -> review -> resume.

Pure polling logic over an httpx client so it can be tested with a mock transport.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

TERMINAL = {"completed", "failed", "error"}


@dataclass
class SmokeResult:
    ok: bool
    steps: list[str] = field(default_factory=list)
    case: dict[str, Any] | None = None
    error: str | None = None


async def wait_for_status(
    get_case: Callable[[], Awaitable[dict[str, Any]]],
    wanted: set[str],
    timeout: float,
    interval: float = 2.0,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> dict[str, Any]:
    """Poll until the case status is in `wanted`; raises TimeoutError otherwise."""
    deadline = clock() + timeout
    while True:
        case = await get_case()
        if case["status"] in wanted:
            return case
        if clock() >= deadline:
            raise TimeoutError(f"case {case.get('case_id')} stuck in {case['status']}")
        await sleep(interval)


async def run_smoke(
    client: httpx.AsyncClient,
    alert_id: int,
    analyst_id: str
    | None = "smoke-test",  # None: the API takes it from the token (F11)
    timeout: float = 300,
    interval: float = 2.0,
) -> SmokeResult:
    res = SmokeResult(ok=False)
    try:
        ready = (await client.get("/ready")).json()
        res.steps.append(f"ready: {ready.get('status')}")
        if ready.get("status") not in ("ok", "degraded"):
            res.error = f"/ready said {ready}"
            return res

        r = await client.post(f"/alerts/{alert_id}/investigations")
        r.raise_for_status()
        case_id = r.json()["case_id"]
        res.steps.append(f"case {case_id} created ({r.status_code})")

        async def get_case() -> dict[str, Any]:
            g = await client.get(f"/cases/{case_id}")
            g.raise_for_status()
            return g.json()  # type: ignore[no-any-return]

        case = await wait_for_status(
            get_case, {"awaiting_review"} | TERMINAL, timeout, interval
        )
        res.steps.append(f"investigation finished: {case['status']}")

        if case["status"] == "awaiting_review":
            d = await client.post(
                f"/cases/{case_id}/decision",
                json={
                    "decision": "approve",
                    **({"analyst_id": analyst_id} if analyst_id else {}),
                    "notes": "F17 smoke test",
                },
            )
            d.raise_for_status()
            res.steps.append("decision approve accepted")
            case = await wait_for_status(get_case, TERMINAL, timeout, interval)
            res.steps.append(f"resumed to: {case['status']}")

        res.case = case
        res.ok = case["status"] == "completed"
        if not res.ok:
            res.error = f"case ended as {case['status']}"
    except (httpx.HTTPError, TimeoutError, KeyError) as exc:
        res.error = repr(exc)
    return res

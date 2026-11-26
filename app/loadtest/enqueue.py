"""Send the load set to the API (F16): `POST /alerts/{id}/investigations`.

`burst` fires everything at once (bounded by `concurrency` in-flight requests) and
measures how fast the queue drains. `steady` sends at a constant `rate` per second and
measures latency under sustained load. Both record API latency and errors.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Literal

import httpx

Profile = Literal["burst", "steady"]


@dataclass
class EnqueueResult:
    total: int = 0
    errors: int = 0
    latencies_ms: list[float] = field(default_factory=list)  # successful calls only
    started: float = 0.0  # monotonic seconds
    ended: float = 0.0
    error_samples: list[str] = field(default_factory=list)


async def _one(client: httpx.AsyncClient, alert_id: int, out: EnqueueResult) -> None:
    started = time.monotonic()
    out.total += 1
    try:
        resp = await client.post(f"/alerts/{alert_id}/investigations")
        if resp.status_code in (200, 202):
            out.latencies_ms.append((time.monotonic() - started) * 1000)
            return
        out.errors += 1
        if len(out.error_samples) < 5:
            out.error_samples.append(f"{alert_id}: HTTP {resp.status_code}")
    except httpx.HTTPError as exc:
        out.errors += 1
        if len(out.error_samples) < 5:
            out.error_samples.append(f"{alert_id}: {exc!r}")


async def enqueue_all(
    client: httpx.AsyncClient,
    alert_ids: Sequence[int],
    profile: Profile = "burst",
    rate: float = 10.0,
    concurrency: int = 50,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> EnqueueResult:
    if profile == "steady" and rate <= 0:
        raise ValueError("steady profile needs rate > 0")
    out = EnqueueResult(started=time.monotonic())
    tasks: list[asyncio.Task[None]] = []
    if profile == "burst":
        gate = asyncio.Semaphore(concurrency)

        async def bounded(a: int) -> None:
            async with gate:
                await _one(client, a, out)

        tasks = [asyncio.create_task(bounded(a)) for a in alert_ids]
    else:
        for i, a in enumerate(alert_ids):
            tasks.append(asyncio.create_task(_one(client, a, out)))
            if i < len(alert_ids) - 1:
                await sleep(1 / rate)
    await asyncio.gather(*tasks)
    out.ended = time.monotonic()
    return out

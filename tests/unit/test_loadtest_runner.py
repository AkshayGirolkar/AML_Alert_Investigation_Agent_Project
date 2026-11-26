"""F16 unit tests: enqueue, drain wait, sampler, report. No DB, queue or network."""

import asyncio
import json

import httpx
import pytest

from app.loadtest.collect import wait_for_drain
from app.loadtest.enqueue import enqueue_all
from app.loadtest.metrics import CaseRow, api_stats, compute_run_metrics
from app.loadtest.report import (
    build_result,
    is_clean,
    render_summary,
    worker_config,
)
from app.loadtest.sampler import Sampler, parse_docker_stats


def client(handler: httpx.MockTransport) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=handler, base_url="http://api")


async def test_burst_sends_every_alert_once_and_bounds_concurrency() -> None:
    seen: list[str] = []
    inflight = peak = 0

    async def handler(req: httpx.Request) -> httpx.Response:
        nonlocal inflight, peak
        inflight += 1
        peak = max(peak, inflight)
        await asyncio.sleep(0.01)
        inflight -= 1
        seen.append(req.url.path)
        return httpx.Response(202, json={})

    async with client(httpx.MockTransport(handler)) as c:
        out = await enqueue_all(c, list(range(1, 21)), "burst", concurrency=4)
    assert sorted(seen) == sorted(f"/alerts/{i}/investigations" for i in range(1, 21))
    assert out.total == 20 and out.errors == 0 and len(out.latencies_ms) == 20
    assert peak <= 4


async def test_errors_are_counted_not_raised() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.startswith("/alerts/2/"):
            return httpx.Response(500)
        if req.url.path.startswith("/alerts/3/"):
            raise httpx.ConnectError("boom")
        return httpx.Response(200 if "/alerts/4/" in req.url.path else 202)

    async with client(httpx.MockTransport(handler)) as c:
        out = await enqueue_all(c, [1, 2, 3, 4], "burst")
    assert (out.total, out.errors, len(out.latencies_ms)) == (4, 2, 2)  # 200 is ok
    assert len(out.error_samples) == 2


async def test_steady_paces_requests() -> None:
    slept: list[float] = []

    async def fake_sleep(s: float) -> None:
        slept.append(s)

    async with client(httpx.MockTransport(lambda r: httpx.Response(202))) as c:
        out = await enqueue_all(c, [1, 2, 3, 4], "steady", rate=4, sleep=fake_sleep)
    assert out.total == 4 and slept == [0.25, 0.25, 0.25]  # none after the last


async def test_steady_needs_a_rate() -> None:
    async with client(httpx.MockTransport(lambda r: httpx.Response(202))) as c:
        with pytest.raises(ValueError):
            await enqueue_all(c, [1], "steady", rate=0)


async def test_wait_for_drain_outcomes() -> None:
    remaining = [3, 2, 1, 0]

    async def unfinished() -> int:
        return remaining.pop(0)

    async def nap(_: float) -> None:
        return None

    assert await wait_for_drain(unfinished, 100, sleep=nap) == "drained"

    async def stuck() -> int:
        return 5

    t = iter(range(0, 1000, 10))
    assert (
        await wait_for_drain(stuck, 25, sleep=nap, clock=lambda: float(next(t)))
        == "timeout"
    )

    async def yes() -> bool:
        return True

    assert await wait_for_drain(stuck, 100, sleep=nap, abort=yes) == "aborted"


async def test_sampler_records_probe_results_and_survives_a_failing_probe() -> None:
    async def good() -> dict[str, int]:
        return {"x": 1}

    async def bad() -> dict[str, int]:
        raise RuntimeError("down")

    s = Sampler({"good": good, "bad": bad}, interval=0.01, clock=lambda: 7.0)
    s.start()
    await asyncio.sleep(0.05)
    samples = await s.stop()
    assert len(samples) >= 2
    assert samples[0]["good"] == {"x": 1} and samples[0]["t"] == 7.0
    assert "RuntimeError" in samples[0]["bad"]["error"]


def test_parse_docker_stats() -> None:
    out = "\n".join(
        json.dumps({"Name": n, "CPUPerc": c, "MemPerc": m})
        for n, c, m in (("aml-worker", "45.20%", "3.1%"), ("aml-api", "1.0%", "0.5%"))
    )
    assert parse_docker_stats(out + "\n") == {
        "aml-worker": {"cpu_pct": 45.2, "mem_pct": 3.1},
        "aml-api": {"cpu_pct": 1.0, "mem_pct": 0.5},
    }
    assert parse_docker_stats("") == {}


def test_worker_config_picks_only_known_keys() -> None:
    env = [
        "PATH=/bin",
        "LLM_PROVIDER=fake",
        "WORKER_CONCURRENCY=8",
        "OPENAI_API_KEY=sk",
    ]
    assert worker_config(env) == {"LLM_PROVIDER": "fake", "WORKER_CONCURRENCY": "8"}


def _result(dlq: int = 0, dup: bool = False, outcome: str = "drained") -> dict:
    rows = [
        CaseRow(
            i, "completed", 0, 5 + i, 20 + i, plan_calls_ok=2 if dup and i == 0 else 1
        )
        for i in range(10)
    ]
    m = compute_run_metrics(rows, dlq=dlq)
    return build_result(
        "r1",
        "abc",
        "burst",
        {"run": {"count": 10}, "worker": {"LLM_PROVIDER": "fake"}},
        m,
        api_stats([5, 6], 0, 2),
        outcome,
        [{"t": 1}],
    )


def test_is_clean_rules() -> None:
    assert is_clean(_result())
    assert not is_clean(_result(dup=True))
    assert not is_clean(_result(dlq=1))
    assert is_clean(_result(dlq=1), allow_dlq=True)
    assert not is_clean(_result(outcome="timeout"))


def test_result_is_json_serialisable_and_summary_has_config() -> None:
    r = _result()
    json.dumps(r)
    md = render_summary(r)
    assert "Load run r1" in md and "LLM_PROVIDER" in md and "duplicates **0**" in md
    assert "n/a" not in md.split("| Latency")[0]  # headline numbers are present

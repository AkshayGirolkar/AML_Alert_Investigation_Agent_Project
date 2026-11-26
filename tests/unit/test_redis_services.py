"""F10 unit tests: cache, token bucket, lock, events, SSE, processor wiring (fakeredis)."""

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import fakeredis
import httpx
import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from app.agent.llm import FakeLLM, RateLimitedLLM
from app.agent.schemas import Verdict
from app.main import create_app
from app.services.events import EventPublisher, events_key
from app.services.locks import RedisLock, case_lock_key
from app.services.rate_limiter import (
    RateLimitTimeout,
    RedisTokenBucket,
)
from app.services.sanctions_cache import SanctionsCache, bump_version
from app.worker.processor import Processor
from tests.unit.test_worker_processor import FakeGraph, FakeQueue, FakeStore, _msg


@pytest.fixture
async def redis() -> Any:
    r = fakeredis.FakeAsyncRedis(decode_responses=True)
    yield r
    await r.flushall()
    await r.aclose()


class BrokenRedis:
    """Every command fails like an unreachable server."""

    def __getattr__(self, name: str) -> Any:
        async def boom(*a: Any, **k: Any) -> Any:
            raise RedisConnectionError("down")

        return boom


# ---- sanctions cache ------------------------------------------------------------
async def test_cache_computes_once_then_hits(redis: Any) -> None:
    cache, calls = SanctionsCache(redis, 60), []

    async def compute() -> list[dict[str, Any]]:
        calls.append(1)
        return [{"source_id": "x", "sim": 0.9}]

    first = await cache.get_or_compute("Ivan  Petrov", 0.5, compute)
    second = await cache.get_or_compute("ivan petrov", 0.5, compute)  # normalised
    assert first == ([{"source_id": "x", "sim": 0.9}], False)
    assert second == ([{"source_id": "x", "sim": 0.9}], True)
    assert len(calls) == 1


async def test_cache_keys_differ_by_threshold_and_version_bump_invalidates(
    redis: Any,
) -> None:
    cache, calls = SanctionsCache(redis, 60), []

    async def compute() -> list[dict[str, Any]]:
        calls.append(1)
        return []

    await cache.get_or_compute("a", 0.5, compute)
    await cache.get_or_compute("a", 0.7, compute)
    assert len(calls) == 2
    await bump_version(redis)
    _, cached = await cache.get_or_compute("a", 0.5, compute)
    assert not cached and len(calls) == 3


async def test_cache_entries_expire(redis: Any) -> None:
    cache = SanctionsCache(redis, 60)

    async def compute() -> list[dict[str, Any]]:
        return []

    await cache.get_or_compute("a", 0.5, compute)
    keys = [k async for k in redis.scan_iter("sanctions:0:*")]
    assert len(keys) == 1 and 0 < await redis.ttl(keys[0]) <= 60


async def test_cache_falls_back_to_compute_when_redis_down() -> None:
    cache = SanctionsCache(BrokenRedis(), 60)  # type: ignore[arg-type]

    async def compute() -> list[dict[str, Any]]:
        return [{"source_id": "y"}]

    assert await cache.get_or_compute("a", 0.5, compute) == (
        [{"source_id": "y"}],
        False,
    )


# ---- token bucket ---------------------------------------------------------------
async def test_bucket_allows_burst_then_denies(redis: Any) -> None:
    b = RedisTokenBucket(redis, "rl:t1", rate_per_second=1, burst=3)
    assert [await b.try_acquire() for _ in range(3)] == [0.0, 0.0, 0.0]
    assert await b.try_acquire() > 0


async def test_bucket_refills_over_time(redis: Any) -> None:
    b = RedisTokenBucket(redis, "rl:t2", rate_per_second=50, burst=1)
    assert await b.try_acquire() == 0.0
    assert await b.try_acquire() > 0
    await asyncio.sleep(0.05)
    assert await b.try_acquire() == 0.0


async def test_bucket_shared_between_clients_never_overspends(redis: Any) -> None:
    a = RedisTokenBucket(redis, "rl:shared", rate_per_second=0.01, burst=5)
    b = RedisTokenBucket(redis, "rl:shared", rate_per_second=0.01, burst=5)
    results = await asyncio.gather(
        *[(a if i % 2 else b).try_acquire() for i in range(20)]
    )
    assert sum(1 for r in results if r == 0.0) == 5


async def test_acquire_times_out_instead_of_waiting_forever(redis: Any) -> None:
    b = RedisTokenBucket(redis, "rl:t3", 0.001, 1, max_wait_seconds=0.2)
    await b.acquire()
    with pytest.raises(RateLimitTimeout):
        await b.acquire()


async def test_acquire_fails_open_when_redis_down() -> None:
    b = RedisTokenBucket(BrokenRedis(), "rl", 1, 1)  # type: ignore[arg-type]
    await b.acquire()  # does not raise


async def test_rate_limited_llm_acquires_then_delegates() -> None:
    class Limiter:
        n = 0

        async def acquire(self, cost: int = 1) -> None:
            self.n += 1

    inner = FakeLLM(Verdict.model_construct())
    lim = Limiter()
    out = await RateLimitedLLM(inner, lim).structured(Verdict, [("user", "x")])
    assert lim.n == 1 and out is not None and len(inner.calls) == 1


# ---- lock -----------------------------------------------------------------------
async def test_lock_is_exclusive_and_owner_safe(redis: Any) -> None:
    a, b = RedisLock(redis, "lock:x", 30), RedisLock(redis, "lock:x", 30)
    assert await a.acquire() and not await b.acquire()
    await b.release()  # not the owner: must not free it
    assert await redis.exists("lock:x")
    await a.release()
    assert await b.acquire()


async def test_lock_renew_only_for_owner_and_expires(redis: Any) -> None:
    a = RedisLock(redis, "lock:y", 30)
    assert await a.acquire() and await a.renew()
    await redis.delete("lock:y")
    assert not await a.renew()
    c = RedisLock(redis, "lock:z", 1)
    await c.acquire()
    await redis.pexpire("lock:z", 10)
    await asyncio.sleep(0.05)
    assert await RedisLock(redis, "lock:z", 1).acquire()


async def test_lock_degrades_when_redis_down() -> None:
    lock = RedisLock(BrokenRedis(), "lock:q", 30)  # type: ignore[arg-type]
    assert await lock.acquire() and lock.degraded
    await lock.release()


# ---- events + SSE ---------------------------------------------------------------
async def test_publisher_writes_stream_with_ttl_and_never_raises(redis: Any) -> None:
    pub = EventPublisher(redis, ttl_seconds=60)
    await pub.publish(7, "node_finished", {"node": "plan"})
    entries = await redis.xrange(events_key(7))
    assert len(entries) == 1 and entries[0][1]["type"] == "node_finished"
    assert json.loads(entries[0][1]["data"]) == {"node": "plan"}
    assert 0 < await redis.ttl(events_key(7)) <= 60
    await EventPublisher(BrokenRedis()).publish(7, "x")  # type: ignore[arg-type]


class _Store:
    def __init__(self, status: str) -> None:
        self.status = status

    async def get(self, case_id: int) -> dict[str, Any] | None:
        return None if case_id == 404 else {"case_id": case_id, "status": self.status}


def _sse_client(store: _Store, redis: Any) -> httpx.AsyncClient:
    fake: Any = object()
    app = create_app(store=store, queue=fake, redis=redis)  # type: ignore[arg-type]
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    )


def _events(body: str) -> list[tuple[str, dict[str, Any]]]:
    out = []
    for block in body.strip().split("\n\n"):
        lines = dict(l.split(": ", 1) for l in block.splitlines() if ": " in l)
        if "event" in lines:
            out.append((lines["event"], json.loads(lines["data"])))
    return out


async def test_sse_replays_events_and_closes_on_terminal_status(redis: Any) -> None:
    pub = EventPublisher(redis)
    await pub.publish(5, "started", {})
    await pub.publish(5, "node_finished", {"node": "plan"})
    await pub.publish(5, "status", {"status": "completed"})
    async with _sse_client(_Store("completed"), redis) as c:
        r = await asyncio.wait_for(c.get("/cases/5/events"), 5)
    assert r.headers["content-type"].startswith("text/event-stream")
    assert [e[0] for e in _events(r.text)] == [
        "status",
        "started",
        "node_finished",
        "status",
    ]


async def test_sse_honours_last_event_id(redis: Any) -> None:
    pub = EventPublisher(redis)
    await pub.publish(6, "started", {})
    first_id = (await redis.xrange(events_key(6)))[0][0]
    await pub.publish(6, "status", {"status": "failed"})
    async with _sse_client(_Store("failed"), redis) as c:
        r = await asyncio.wait_for(
            c.get("/cases/6/events", headers={"Last-Event-ID": first_id}), 5
        )
    assert [e[0] for e in _events(r.text)] == ["status", "status"]


async def test_sse_404_for_unknown_case(redis: Any) -> None:
    async with _sse_client(_Store("queued"), redis) as c:
        assert (await c.get("/cases/404/events")).status_code == 404


# ---- processor wiring -----------------------------------------------------------
class StreamingGraph(FakeGraph):
    def __init__(self) -> None:
        super().__init__()
        self.snapshot = SimpleNamespace(
            next=(), values={"outcome": "closed", "verdict": {}}, tasks=()
        )
        self._calls = 0

    async def astream(self, payload: Any, config: Any, stream_mode: str) -> Any:
        for node in ("load_alert", "plan"):
            yield {node: {}}

    async def aget_state(self, config: dict[str, Any]) -> SimpleNamespace:
        # first call (before running): nothing yet; afterwards: the final state
        self._calls += 1
        if self._calls == 1:
            return SimpleNamespace(next=(), values={}, tasks=())
        return self.snapshot


async def test_processor_publishes_progress_and_releases_lock(redis: Any) -> None:
    store: Any = FakeStore()
    queue: Any = FakeQueue()
    graph = StreamingGraph()
    proc = Processor(
        graph,
        store,
        queue,
        3,
        300,
        redis=redis,
        events=EventPublisher(redis),
    )
    assert await proc.handle(_msg()) == "completed"
    types = [
        (f["type"], json.loads(f["data"])) for _, f in await redis.xrange(events_key(1))
    ]
    assert [t for t, _ in types] == [
        "started",
        "node_finished",
        "node_finished",
        "status",
    ]
    assert types[-1][1] == {"status": "completed"}
    assert not await redis.exists(case_lock_key(1))


async def test_processor_leaves_message_when_case_is_locked(redis: Any) -> None:
    await redis.set(case_lock_key(1), "someone-else", px=60_000)
    store: Any = FakeStore()
    queue: Any = FakeQueue()
    graph = FakeGraph()
    proc = Processor(graph, store, queue, 3, 300, redis=redis)
    assert await proc.handle(_msg()) == "retry"
    assert queue.deleted == [] and graph.invocations == []
    assert await redis.get(case_lock_key(1)) == "someone-else"


def test_sse_block_time_stays_below_the_redis_socket_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api.routes import _block_ms

    for timeout in (0.6, 2.0, 30.0):
        monkeypatch.setenv("REDIS_SOCKET_TIMEOUT", str(timeout))
        assert _block_ms() < timeout * 1000 or timeout < 0.6

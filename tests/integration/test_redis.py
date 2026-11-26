"""F10 integration tests against a real Redis (docker compose up -d redis); skipped if absent."""

import asyncio
import uuid
from collections.abc import AsyncIterator

import pytest
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.config import get_settings
from app.services.locks import RedisLock
from app.services.rate_limiter import RedisTokenBucket
from app.services.sanctions_cache import SanctionsCache, bump_version


@pytest.fixture
async def redis() -> AsyncIterator[Redis]:
    r = Redis.from_url(
        get_settings().redis_url, decode_responses=True, socket_connect_timeout=2
    )
    try:
        await asyncio.wait_for(r.ping(), timeout=3)
    except (OSError, TimeoutError, RedisError):
        await r.aclose()
        pytest.skip("Redis is not reachable")
    yield r
    await r.aclose()


async def test_two_clients_race_for_one_lock(redis: Redis) -> None:
    key = f"lock:test:{uuid.uuid4().hex}"
    a, b = RedisLock(redis, key, 10), RedisLock(redis, key, 10)
    got = await asyncio.gather(a.acquire(), b.acquire())
    assert sorted(got) == [False, True]
    await a.release()
    await b.release()
    assert not await redis.exists(key)


async def test_token_bucket_is_shared_across_clients(redis: Redis) -> None:
    key = f"ratelimit:test:{uuid.uuid4().hex}"
    buckets = [RedisTokenBucket(redis, key, 0.01, 4) for _ in range(2)]
    res = await asyncio.gather(*[buckets[i % 2].try_acquire() for i in range(12)])
    assert sum(1 for r in res if r == 0.0) == 4
    await redis.delete(key)


async def test_cache_hit_on_second_lookup_and_bump_invalidates(redis: Redis) -> None:
    cache, n = SanctionsCache(redis, 30), 0
    name = f"name-{uuid.uuid4().hex}"

    async def compute() -> list[dict[str, object]]:
        nonlocal n
        n += 1
        return [{"source_id": "s", "sim": 0.8}]

    await cache.get_or_compute(name, 0.5, compute)
    _, hit = await cache.get_or_compute(name, 0.5, compute)
    assert hit and n == 1
    await bump_version(redis)  # leaves the shared version counter higher: harmless
    _, hit = await cache.get_or_compute(name, 0.5, compute)
    assert not hit and n == 2

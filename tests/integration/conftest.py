"""Shared integration fixtures."""

import asyncio
from collections.abc import AsyncIterator

import pytest
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.config import get_settings


@pytest.fixture
async def real_redis() -> AsyncIterator[Redis]:
    """A real Redis (F10), flushed around the test; skips when none is reachable."""
    r = Redis.from_url(
        get_settings().redis_url, decode_responses=True, socket_connect_timeout=2
    )
    try:
        await asyncio.wait_for(r.ping(), timeout=3)
    except (OSError, TimeoutError, RedisError):
        await r.aclose()
        pytest.skip("Redis is not reachable")
    await r.flushdb()
    yield r
    await r.flushdb()
    await r.aclose()

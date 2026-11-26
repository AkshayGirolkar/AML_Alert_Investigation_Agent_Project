"""Redis cache for the sanctions name search (F10).

Caches the expensive part of `sanctions_screen` (trigram + alias scan), which depends only
on (screened name, min_similarity). Entries expire after a TTL and carry a version number
that `bump_version` increments when the sanctions list is reloaded. Any Redis error is a
cache miss: the tool falls back to Postgres.
"""

import hashlib
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError

log = logging.getLogger("sanctions_cache")

VERSION_KEY = "sanctions:version"
Hits = list[dict[str, Any]]


class SanctionsCache:
    def __init__(self, redis: Redis, ttl_seconds: int = 3600) -> None:
        self._redis = redis
        self._ttl = ttl_seconds

    @staticmethod
    def _digest(name: str, min_similarity: float) -> str:
        norm = " ".join(name.casefold().split())
        return hashlib.sha1(f"{norm}|{min_similarity}".encode()).hexdigest()

    async def get_or_compute(
        self,
        name: str,
        min_similarity: float,
        compute: Callable[[], Awaitable[Hits]],
    ) -> tuple[Hits, bool]:
        """Return (hits, was_cached). `compute` runs on a miss or any Redis failure."""
        key: str | None = None
        try:
            version = str(await self._redis.get(VERSION_KEY) or "0")
            key = f"sanctions:{version}:{self._digest(name, min_similarity)}"
            raw = await self._redis.get(key)
            if raw is not None:
                return json.loads(raw), True
        except (RedisError, OSError, ValueError):
            log.warning("sanctions cache read failed; using Postgres", exc_info=True)
            key = None
        hits = await compute()
        if key is not None:
            try:
                await self._redis.set(key, json.dumps(hits, default=str), ex=self._ttl)
            except (RedisError, OSError):
                log.warning("sanctions cache write failed", exc_info=True)
        return hits, False


async def bump_version(redis: Redis) -> int:
    """Invalidate every cached sanctions lookup (call after reloading the list)."""
    return int(await redis.incr(VERSION_KEY))

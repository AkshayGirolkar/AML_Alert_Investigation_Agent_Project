"""Redis case lock (F10): `SET NX PX` with an owner token.

A second guard on top of the Postgres claim (`SqlCaseStore.claim`): it stops a worker that
took over a stale claim from racing one that is merely slow. Release and renew only act on
a lock we still own. If Redis is unreachable the lock degrades to "held" and the Postgres
claim alone protects the case.

A per-customer lock around recall_history..write_memory is deliberately not provided: that
window spans the human-review interrupt (hours), so a lock cannot cover it (ADR 0004).
"""

import logging
import uuid

from redis.asyncio import Redis
from redis.exceptions import RedisError

log = logging.getLogger("locks")

_RELEASE = """
if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) end
return 0
"""
_RENEW = """
if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('PEXPIRE', KEYS[1], ARGV[2]) end
return 0
"""


def case_lock_key(case_id: int) -> str:
    return f"lock:case:{case_id}"


class RedisLock:
    def __init__(self, redis: Redis, key: str, ttl_seconds: int) -> None:
        self._redis = redis
        self._key = key
        self._ttl_ms = ttl_seconds * 1000
        self._token = uuid.uuid4().hex
        self.degraded = False  # True when Redis was down and we proceeded without it

    async def acquire(self) -> bool:
        try:
            return bool(
                await self._redis.set(self._key, self._token, nx=True, px=self._ttl_ms)
            )
        except (RedisError, OSError):
            log.warning("lock %s: Redis unavailable; relying on DB claim", self._key)
            self.degraded = True
            return True

    async def renew(self) -> bool:
        if self.degraded:
            return True
        try:
            return bool(
                await self._redis.eval(  # type: ignore[misc]
                    _RENEW, 1, self._key, self._token, self._ttl_ms
                )
            )
        except (RedisError, OSError):
            log.warning("lock %s: renew failed", self._key)
            return True

    async def release(self) -> None:
        if self.degraded:
            return
        try:
            await self._redis.eval(_RELEASE, 1, self._key, self._token)  # type: ignore[misc]
        except (RedisError, OSError):
            log.warning("lock %s: release failed (will expire)", self._key)

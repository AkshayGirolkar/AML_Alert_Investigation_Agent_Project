"""Distributed token-bucket limiter (F10), shared by every worker through Redis.

Refill and take happen in one Lua script, using the Redis server clock, so concurrent
workers never over-spend and clock skew between hosts doesn't matter. If Redis is
unreachable the limiter fails open: an outage must not stop investigations.
"""

import asyncio
import logging
import random

from redis.asyncio import Redis
from redis.exceptions import RedisError

log = logging.getLogger("rate_limiter")

# KEYS[1]=bucket; ARGV: rate/sec, burst, cost. Returns {allowed, wait_ms}.
_LUA = """
local t = redis.call('TIME')
local now = t[1] * 1000 + math.floor(t[2] / 1000)
local rate, burst, cost = tonumber(ARGV[1]), tonumber(ARGV[2]), tonumber(ARGV[3])
local d = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local tokens, ts = tonumber(d[1]), tonumber(d[2])
if tokens == nil then tokens = burst; ts = now end
tokens = math.min(burst, tokens + (now - ts) * rate / 1000)
local allowed, wait = 0, 0
if tokens >= cost then
  tokens = tokens - cost; allowed = 1
else
  wait = math.ceil((cost - tokens) * 1000 / rate)
end
redis.call('HSET', KEYS[1], 'tokens', tostring(tokens), 'ts', tostring(now))
redis.call('PEXPIRE', KEYS[1], math.ceil(burst / rate * 1000) * 2 + 1000)
return {allowed, wait}
"""


class RateLimitTimeout(Exception):
    pass


class RedisTokenBucket:
    def __init__(
        self,
        redis: Redis,
        key: str,
        rate_per_second: float,
        burst: int,
        max_wait_seconds: float = 60.0,
    ) -> None:
        if rate_per_second <= 0 or burst < 1:
            raise ValueError("rate_per_second must be > 0 and burst >= 1")
        self._redis = redis
        self._key = key
        self._rate = rate_per_second
        self._burst = burst
        self._max_wait = max_wait_seconds

    async def try_acquire(self, cost: int = 1) -> float:
        """0.0 if the tokens were taken, otherwise seconds to wait before retrying."""
        allowed, wait_ms = await self._redis.eval(  # type: ignore[misc]
            _LUA, 1, self._key, self._rate, self._burst, cost
        )
        return 0.0 if int(allowed) else int(wait_ms) / 1000

    async def acquire(self, cost: int = 1) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._max_wait
        while True:
            try:
                wait = await self.try_acquire(cost)
            except (RedisError, OSError):
                log.warning("rate limiter unavailable; failing open", exc_info=True)
                return
            if wait == 0.0:
                return
            if loop.time() + wait > deadline:
                raise RateLimitTimeout(
                    f"no LLM rate-limit token within {self._max_wait}s"
                )
            await asyncio.sleep(wait + random.uniform(0, 0.05))

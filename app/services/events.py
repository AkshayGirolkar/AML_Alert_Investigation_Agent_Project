"""Per-case progress events (F10): a Redis Stream per case, read by the SSE endpoint.

A stream (not bare pub/sub) so a client that connects after the worker has started, or
reconnects with `Last-Event-ID`, still gets the events it missed. Publishing is
best-effort and never raises: progress reporting must not fail an investigation.
"""

import json
import logging
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError

log = logging.getLogger("events")

TERMINAL_STATUSES = frozenset({"completed", "failed", "awaiting_review"})


def events_key(case_id: int) -> str:
    return f"case:{case_id}:events"


class EventPublisher:
    def __init__(
        self, redis: Redis, ttl_seconds: int = 3600, maxlen: int = 200
    ) -> None:
        self._redis = redis
        self._ttl = ttl_seconds
        self._maxlen = maxlen

    async def publish(
        self, case_id: int, type_: str, data: dict[str, Any] | None = None
    ) -> None:
        key = events_key(case_id)
        try:
            await self._redis.xadd(
                key,
                {"type": type_, "data": json.dumps(data or {}, default=str)},
                maxlen=self._maxlen,
                approximate=True,
            )
            await self._redis.expire(key, self._ttl)
        except (RedisError, OSError):
            log.warning("could not publish %s for case %s", type_, case_id)


def parse_entry(entry_id: str, fields: dict[str, str]) -> dict[str, Any]:
    return {
        "id": entry_id,
        "type": fields.get("type", "message"),
        "data": json.loads(fields.get("data", "{}")),
    }


def format_sse(event_id: str | None, type_: str, data: dict[str, Any]) -> str:
    head = f"id: {event_id}\n" if event_id else ""
    return f"{head}event: {type_}\ndata: {json.dumps(data, default=str)}\n\n"

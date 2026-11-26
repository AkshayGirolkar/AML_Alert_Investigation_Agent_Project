"""Shared Redis client (F10). Short timeouts so an outage fails fast instead of hanging."""

from redis.asyncio import Redis

from app.config import Settings


def make_redis(settings: Settings) -> Redis:
    return Redis.from_url(  # type: ignore[no-any-return]
        settings.redis_url,
        decode_responses=True,
        socket_connect_timeout=settings.redis_socket_timeout,
        socket_timeout=settings.redis_socket_timeout,
    )

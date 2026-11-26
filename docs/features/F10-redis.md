# F10 – Redis

## Goal
Use Redis for four jobs without making investigations depend on it: sanctions-screening
cache, a shared LLM token-bucket limiter, case locks, and SSE progress streaming.

## In scope
- `redis` service in docker-compose; `REDIS_URL` and F10 settings in `app/config.py` / `.env.example`.
- `SanctionsCache` (TTL + version key bumped by `scripts/load_sanctions.py`), used by `sanctions_screen`.
- `RedisTokenBucket` (atomic Lua) and `RateLimitedLLM` wrapping the worker's plan/assess LLMs.
- `RedisLock` around each case run in `Processor.handle`, renewed by the heartbeat.
- `EventPublisher` (Redis Stream per case) and `GET /cases/{id}/events` (SSE).

## Out of scope
Per-customer lock across `recall_history`..`write_memory` (spans the human-review interrupt),
token/cost-based limiting and provider fallback (F12), auth on the SSE endpoint (F11).

## Interfaces
- `SanctionsCache.get_or_compute(name, min_similarity, compute) -> (hits, was_cached)`
- `RedisTokenBucket.acquire(cost=1)`; raises `RateLimitTimeout` after `llm_rate_limit_max_wait_seconds`.
- `RedisLock.acquire/renew/release`; key `lock:case:{case_id}`.
- Stream `case:{case_id}:events`; event types `started`, `node_finished`, `status`.
- Failure policy: cache -> Postgres, limiter -> fail open, lock -> rely on DB claim, events -> dropped.

## Acceptance criteria
- A repeat sanctions lookup for the same name/threshold is served from Redis; reloading the list invalidates it.
- Concurrent limiters on the same key never exceed burst; waiting callers give up after the max wait.
- A second worker cannot run a case whose lock is held; a non-owner cannot release or renew it.
- SSE replays missed events (`Last-Event-ID`), starts with current status, ends on a terminal status.
- With Redis down, cache/limiter/lock/events degrade as above and the investigation still completes.
- ruff, mypy and pytest pass (integration tests skip without Redis).

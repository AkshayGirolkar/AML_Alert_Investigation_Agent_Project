"""Async worker.  python -m app.worker

Polls SQS and runs up to `worker_concurrency` investigations at once. SIGTERM/SIGINT
stop polling and wait for in-flight investigations; messages of anything cut short by a
hard kill reappear after the visibility timeout and resume from their checkpoint.
"""

import asyncio
import logging
import signal

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.store.postgres import AsyncPostgresStore
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from sqlalchemy.ext.asyncio import create_async_engine

from app.agent.gateway import build_gateway
from app.agent.graph import build_graph
from app.agent.llm import LLM
from app.agent.tools.registry import make_conn_factory
from app.agent.usage_store import SqlUsageRecorder
from app.config import Settings, get_settings
from app.db.cases import SqlCaseStore
from app.db.copy import conninfo
from app.messaging.sqs import InvestigationQueue, open_queue
from app.observability.logging import configure_logging
from app.observability.metrics import metrics
from app.observability.tracing import setup_tracing
from app.redis_client import make_redis
from app.services.events import EventPublisher
from app.services.rate_limiter import RedisTokenBucket
from app.services.sanctions_cache import SanctionsCache
from app.synth.embeddings import OpenAIEmbedder
from app.worker.console_relay import ConsoleRelay
from app.worker.processor import Processor

log = logging.getLogger("worker")
POLL_WAIT_SECONDS = 10


async def poll_loop(
    queue: InvestigationQueue,
    processor: Processor,
    concurrency: int,
    stop: asyncio.Event,
) -> None:
    inflight: set[asyncio.Task[object]] = set()
    while not stop.is_set():
        free = concurrency - len(inflight)
        if free <= 0:
            _, inflight = await asyncio.wait(
                inflight, return_when=asyncio.FIRST_COMPLETED
            )
            continue
        try:
            messages = await queue.receive(min(free, 10), POLL_WAIT_SECONDS)
        except Exception:
            log.exception("receive failed; retrying")
            await asyncio.sleep(2)
            continue
        for msg in messages:
            task = asyncio.create_task(processor.handle(msg))
            inflight.add(task)
            task.add_done_callback(inflight.discard)
    if inflight:
        log.info("shutting down: waiting for %d investigation(s)", len(inflight))
        await asyncio.gather(*inflight, return_exceptions=True)


async def run(settings: Settings) -> None:
    if not settings.openai_api_key:
        raise SystemExit("OPENAI_API_KEY is not set (see .env.example)")
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    engine = create_async_engine(settings.async_database_url)
    pool = AsyncConnectionPool(
        conninfo(settings.sync_database_url),
        kwargs={"autocommit": True, "row_factory": dict_row},
        open=False,
    )
    await pool.open()
    redis = make_redis(settings)
    limiter = RedisTokenBucket(
        redis,
        "ratelimit:llm",
        settings.llm_rate_limit_per_second,
        settings.llm_rate_limit_burst,
        settings.llm_rate_limit_max_wait_seconds,
    )

    recorder = SqlUsageRecorder(engine)

    def llm(node: str, model: str, fallback: str) -> LLM:
        assert settings.openai_api_key
        return build_gateway(settings, node, model, fallback, recorder, limiter)

    try:
        checkpointer = AsyncPostgresSaver(pool)  # type: ignore[arg-type]
        await checkpointer.setup()  # creates the checkpoint tables if missing
        store = AsyncPostgresStore(pool)  # type: ignore[arg-type]
        await store.setup()  # F09 long-term memory tables
        graph = build_graph(
            make_conn_factory(settings.sync_database_url),
            plan_llm=llm(
                "plan", settings.llm_plan_model, settings.llm_fallback_plan_model
            ),
            assess_llm=llm(
                "assess", settings.llm_assess_model, settings.llm_fallback_assess_model
            ),
            embedder=OpenAIEmbedder(settings.openai_api_key),
            settings=settings,
            checkpointer=checkpointer,
            store=store,
            sanctions_cache=SanctionsCache(redis, settings.sanctions_cache_ttl_seconds),
        )
        async with open_queue(settings) as queue:
            processor = Processor(
                graph,
                SqlCaseStore(engine),
                queue,
                settings.sqs_max_receive_count,
                settings.sqs_visibility_timeout,
                redis=redis,
                events=EventPublisher(redis, settings.events_ttl_seconds),
                lock_ttl=settings.case_lock_ttl_seconds,
            )
            log.info("worker started (concurrency=%d)", settings.worker_concurrency)
            metrics_task: asyncio.Task[None] | None = None
            if settings.worker_metrics_port > 0:
                metrics_task = asyncio.create_task(
                    serve_metrics(settings.worker_metrics_port, stop)
                )
            relay_task: asyncio.Task[None] | None = None
            if settings.console_relay_enabled:
                relay = ConsoleRelay(
                    engine,
                    SqlCaseStore(engine),
                    queue,
                    settings.console_relay_max_attempts,
                )
                relay_task = asyncio.create_task(
                    relay.run(
                        conninfo(settings.sync_database_url),
                        settings.console_relay_poll_seconds,
                        stop,
                    )
                )
            try:
                await poll_loop(queue, processor, settings.worker_concurrency, stop)
            finally:
                if relay_task is not None:
                    await relay_task
                if metrics_task is not None:
                    stop.set()
                    await metrics_task
    finally:
        await redis.aclose()
        await pool.close()
        await engine.dispose()


async def serve_metrics(port: int, stop: asyncio.Event) -> None:
    """Tiny HTTP responder for the worker's in-process metrics (GET anything)."""

    async def handle(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
            body = metrics.render().encode()
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/plain; version=0.0.4\r\n"
                b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
            )
            await writer.drain()
        except (TimeoutError, asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "0.0.0.0", port)
    async with server:
        await stop.wait()


def main() -> int:
    settings = get_settings()
    configure_logging(settings.log_format, settings.log_level)
    log.info("langsmith tracing %s", "on" if setup_tracing() else "off")
    asyncio.run(run(settings))
    return 0

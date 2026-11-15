"""Async worker.  python -m app.worker

Polls SQS and runs up to `worker_concurrency` investigations at once. SIGTERM/SIGINT
stop polling and wait for in-flight investigations; messages of anything cut short by a
hard kill reappear after the visibility timeout and resume from their checkpoint.
"""

import asyncio
import logging
import signal

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from sqlalchemy.ext.asyncio import create_async_engine

from app.agent.graph import build_graph
from app.agent.llm import OpenAILLM
from app.agent.tools.registry import make_conn_factory
from app.config import Settings, get_settings
from app.db.cases import SqlCaseStore
from app.db.copy import conninfo
from app.messaging.sqs import InvestigationQueue, open_queue
from app.synth.embeddings import OpenAIEmbedder
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
    try:
        checkpointer = AsyncPostgresSaver(pool)  # type: ignore[arg-type]
        await checkpointer.setup()  # creates the checkpoint tables if missing
        graph = build_graph(
            make_conn_factory(settings.sync_database_url),
            plan_llm=OpenAILLM(settings.llm_plan_model, settings.openai_api_key),
            assess_llm=OpenAILLM(settings.llm_assess_model, settings.openai_api_key),
            embedder=OpenAIEmbedder(settings.openai_api_key),
            settings=settings,
            checkpointer=checkpointer,
        )
        async with open_queue(settings) as queue:
            processor = Processor(
                graph,
                SqlCaseStore(engine),
                queue,
                settings.sqs_max_receive_count,
                settings.sqs_visibility_timeout,
            )
            log.info("worker started (concurrency=%d)", settings.worker_concurrency)
            await poll_loop(queue, processor, settings.worker_concurrency, stop)
    finally:
        await pool.close()
        await engine.dispose()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    asyncio.run(run(get_settings()))
    return 0

"""F07 integration: checkpoint resume and the case lifecycle on the Docker Postgres.

Real graph, real tools, real `AsyncPostgresSaver` and `SqlCaseStore` against `aml_test`;
the LLM is scripted. Skipped when Postgres is not reachable.
"""

from collections.abc import AsyncIterator, Iterator
from typing import Any, TypeVar

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.agent.graph import build_graph
from app.agent.tools.registry import make_conn_factory
from app.config import get_settings
from app.db.cases import SqlCaseStore
from app.db.copy import conninfo
from app.messaging.sqs import QueueMessage
from app.synth.embeddings import FakeEmbedder
from app.worker.processor import Processor, run_graph
from tests.integration.test_agent_graph import CLOSE, ESCALATE, ScriptedLLM, _alert
from tests.integration.test_rules import TEST_DB, World, _alembic, _conn

pytestmark = pytest.mark.asyncio
T = TypeVar("T", bound=BaseModel)


@pytest.fixture(scope="module")
def _db() -> str:
    base = make_url(get_settings().sync_database_url)
    admin = base.set(drivername="postgresql").render_as_string(hide_password=False)
    try:
        with psycopg.connect(admin, autocommit=True, connect_timeout=3) as conn:
            if not conn.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s", (TEST_DB,)
            ).fetchone():
                conn.execute(f"CREATE DATABASE {TEST_DB}")
    except psycopg.OperationalError:
        pytest.skip("Postgres is not reachable")
    _alembic("upgrade", "head")
    return base.set(database=TEST_DB).render_as_string(hide_password=False)


@pytest.fixture
def w(_db: str) -> Iterator[World]:
    with _conn(_db) as c:
        c.execute(
            "TRUNCATE alerts, adverse_media, planted_matches, sanctions_entities, "
            "laundering_attempt_transactions, laundering_attempts, kyc_profiles, "
            "transactions, accounts, entities, banks RESTART IDENTITY CASCADE"
        )
    yield World(_db)


@pytest.fixture
async def checkpointer(w: World) -> AsyncIterator[Any]:
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    async with AsyncConnectionPool(
        conninfo(w.url),
        kwargs={"autocommit": True, "row_factory": dict_row},
        open=False,
    ) as pool:
        saver = AsyncPostgresSaver(pool)  # type: ignore[arg-type]
        await saver.setup()
        async with pool.connection() as c:  # thread ids repeat across tests
            await c.execute("TRUNCATE checkpoints, checkpoint_blobs, checkpoint_writes")
        yield saver


@pytest.fixture
async def engine(w: World) -> AsyncIterator[AsyncEngine]:
    url = make_url(w.url).set(drivername="postgresql+asyncpg")
    eng = create_async_engine(url.render_as_string(hide_password=False))
    yield eng
    await eng.dispose()


class CrashOnceLLM(ScriptedLLM):
    """Raises on the first Verdict request, simulating a worker that dies in `assess`."""

    def __init__(self, verdicts: list[dict[str, Any]]) -> None:
        super().__init__(verdicts)
        self.crashed = False

    async def structured(self, schema: type[T], messages: Any) -> T:
        if schema.__name__ == "Verdict" and not self.crashed:
            self.crashed = True
            raise RuntimeError("worker died")
        return await super().structured(schema, messages)


def _graph(w: World, llm: ScriptedLLM, checkpointer: Any) -> Any:
    return build_graph(
        make_conn_factory(w.url),
        plan_llm=llm,
        embedder=FakeEmbedder(),
        settings=get_settings(),
        checkpointer=checkpointer,
    )


async def test_resume_continues_after_the_crashed_node_without_rerunning_earlier_ones(
    w: World, checkpointer: Any
) -> None:
    llm = CrashOnceLLM([ESCALATE])
    graph = _graph(w, llm, checkpointer)
    alert_id = _alert(w)

    with pytest.raises(RuntimeError, match="worker died"):
        await run_graph(graph, alert_id)

    # A *new* graph object stands in for the restarted worker process.
    state = await run_graph(_graph(w, llm, checkpointer), alert_id)

    assert state["outcome"] == "escalated" and state["grounded"] is True
    names = [n for n, _ in llm.prompts]
    assert names.count("InvestigationPlan") == 1  # plan was not run again
    assert state["tool_calls"] == 1 and len(state["evidence"]) == 1  # nor the tools


async def test_finished_thread_is_not_investigated_twice(
    w: World, checkpointer: Any
) -> None:
    llm = ScriptedLLM([CLOSE])
    graph = _graph(w, llm, checkpointer)
    alert_id = _alert(w)
    first = await run_graph(graph, alert_id)
    second = await run_graph(
        graph, alert_id
    )  # e.g. DB write failed, message redelivered
    assert second["verdict"] == first["verdict"]
    assert len([n for n, _ in llm.prompts if n == "Verdict"]) == 1


class _Q:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    async def delete(self, receipt_handle: str) -> None:
        self.deleted.append(receipt_handle)

    async def change_visibility(self, receipt_handle: str, seconds: int) -> None:
        pass


async def test_case_lifecycle_retry_then_complete(
    w: World, checkpointer: Any, engine: AsyncEngine
) -> None:
    store, queue = SqlCaseStore(engine), _Q()
    llm = CrashOnceLLM([ESCALATE])
    alert_id = _alert(w)

    case, created = await store.create_or_get(alert_id)
    again, created_again = await store.create_or_get(alert_id)
    assert created and not created_again and again["case_id"] == case["case_id"]

    proc = Processor(_graph(w, llm, checkpointer), store, queue, 3, 300)  # type: ignore[arg-type]
    first = await proc.handle(QueueMessage("r1", case["case_id"], alert_id, 1))
    assert first == "retry" and queue.deleted == []
    row = await store.get(case["case_id"])
    assert row and row["status"] == "queued" and "worker died" in row["error"]

    second = await proc.handle(QueueMessage("r2", case["case_id"], alert_id, 2))
    assert second == "completed" and queue.deleted == ["r2"]
    row = await store.get(case["case_id"])
    assert row and row["status"] == "completed" and row["attempts"] == 2
    assert row["outcome"] == "escalated" and row["verdict"]["disposition"] == "escalate"
    assert row["error"] is None and row["sar_draft"]["narrative"]

    # Redelivery of a finished case: acked, nothing re-run.
    dup = await proc.handle(QueueMessage("r3", case["case_id"], alert_id, 3))
    assert dup == "skipped" and queue.deleted == ["r2", "r3"]
    assert [n for n, _ in llm.prompts].count("InvestigationPlan") == 1


async def test_exhausted_retries_mark_the_case_failed_and_post_can_requeue_it(
    w: World, checkpointer: Any, engine: AsyncEngine
) -> None:
    store = SqlCaseStore(engine)
    alert_id = _alert(w)
    case, _ = await store.create_or_get(alert_id)

    class AlwaysCrash(CrashOnceLLM):
        async def structured(self, schema: type[T], messages: Any) -> T:
            if schema.__name__ == "Verdict":
                raise RuntimeError("always")
            return await ScriptedLLM.structured(self, schema, messages)

    proc = Processor(_graph(w, AlwaysCrash([]), checkpointer), store, _Q(), 3, 300)  # type: ignore[arg-type]
    assert (
        await proc.handle(QueueMessage("r", case["case_id"], alert_id, 3)) == "failed"
    )
    row = await store.get(case["case_id"])
    assert row and row["status"] == "failed"
    assert (
        await store.claim(case["case_id"], 225) is None
    )  # failed cases are not claimable

    requeued = await store.requeue(case["case_id"])
    assert requeued and requeued["status"] == "queued" and requeued["attempts"] == 0


async def test_running_case_is_claimed_again_only_when_its_worker_is_dead(
    w: World, engine: AsyncEngine
) -> None:
    store = SqlCaseStore(engine)
    case, _ = await store.create_or_get(_alert(w))
    cid = case["case_id"]

    assert (await store.claim(cid, 225)) is not None  # first delivery takes it
    # A duplicate message while the worker is alive must not start a second run.
    assert await store.claim(cid, 225) is None

    # Worker died: nothing touched the row for longer than the staleness window.
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE cases SET updated_at = now() - interval '10 minutes'")
        )
    taken = await store.claim(cid, 225)
    assert taken is not None and taken["attempts"] == 2

    # A heartbeat keeps a long-running case from looking dead.
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE cases SET updated_at = now() - interval '10 minutes'")
        )
    await store.touch(cid)
    assert await store.claim(cid, 225) is None

"""F09 integration: long-term memory with the real graph, `AsyncPostgresStore` and
`AsyncPostgresSaver` on the Docker Postgres (`aml_test`). The LLM is scripted.
Skipped when Postgres is not reachable.
"""

import json
from collections.abc import AsyncIterator, Iterator
from datetime import timedelta
from typing import Any

import psycopg
import pytest
from langgraph.store.postgres import AsyncPostgresStore
from langgraph.types import Command
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from sqlalchemy.engine import make_url

from app.agent.graph import build_graph
from app.agent.tools.registry import make_conn_factory
from app.config import get_settings
from app.db.copy import conninfo
from app.synth.embeddings import FakeEmbedder
from tests.integration.test_agent_graph import (
    CLOSE,
    DAY,
    ESCALATE,
    SECRET_TYPOLOGY,
    ScriptedLLM,
)
from tests.integration.test_rules import TEST_DB, World, _alembic, _conn

pytestmark = pytest.mark.asyncio


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
async def pool(w: World) -> AsyncIterator[AsyncConnectionPool]:
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    async with AsyncConnectionPool(
        conninfo(w.url),
        kwargs={"autocommit": True, "row_factory": dict_row},
        open=False,
    ) as p:
        await AsyncPostgresSaver(p).setup()  # type: ignore[arg-type]
        await AsyncPostgresStore(p).setup()  # type: ignore[arg-type]
        async with p.connection() as c:  # thread ids and keys repeat across tests
            await c.execute("TRUNCATE checkpoints, checkpoint_blobs, checkpoint_writes")
            await c.execute("TRUNCATE store")
        yield p


def _store(pool: AsyncConnectionPool) -> AsyncPostgresStore:
    return AsyncPostgresStore(pool)  # type: ignore[arg-type]


def _alert_on(w: World, account: int, week: int = 0) -> int:
    """An alert on an existing account (so several alerts share one customer).
    `week` shifts the window: (rule, account, window_start) must be unique."""
    start = DAY + timedelta(weeks=week)
    other = w.account()
    txns = [w.txn(account, other, start + timedelta(hours=i), 100.0) for i in range(3)]
    with _conn(w.url) as c:
        row = c.execute(
            "INSERT INTO alerts (rule_id, account_id, window_start, window_end, "
            "trigger_txn_ids, score, is_true_positive, typology) "
            "VALUES (%s, %s, %s, %s, %s, 0.5, true, %s) RETURNING alert_id",
            (
                "high_velocity",
                account,
                start,
                start + timedelta(days=1),
                txns,
                SECRET_TYPOLOGY,
            ),
        ).fetchone()
    assert row is not None
    return int(row[0])


def _graph(w: World, llm: ScriptedLLM, pool: AsyncConnectionPool) -> Any:
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    return build_graph(
        make_conn_factory(w.url),
        plan_llm=llm,
        embedder=FakeEmbedder(),
        settings=get_settings(),
        checkpointer=AsyncPostgresSaver(pool),  # type: ignore[arg-type]
        store=_store(pool),
    )


async def _investigate(
    graph: Any, alert_id: int, decision: str | None = None, notes: str | None = None
) -> dict[str, Any]:
    config = {"configurable": {"thread_id": f"alert-{alert_id}"}}
    state: dict[str, Any] = await graph.ainvoke({"alert_id": alert_id}, config)
    if state.get("__interrupt__") and decision:
        resume = {"decision": decision, "analyst_id": "ana", "notes": notes}
        state = await graph.ainvoke(Command(resume=resume), config)
    return state


async def test_second_alert_uses_prior_outcome_and_cannot_auto_close(
    w: World, pool: AsyncConnectionPool
) -> None:
    acct = w.account()
    first, second = _alert_on(w, acct), _alert_on(w, acct, week=1)

    llm1 = ScriptedLLM([ESCALATE])
    s1 = await _investigate(
        _graph(w, llm1, pool), first, "approve", "payroll pattern, filed SAR"
    )
    assert s1["outcome"] == "escalated" and s1["history"] == []

    # Same customer, benign-looking verdict: the prior approved SAR still blocks auto-close.
    llm2 = ScriptedLLM([CLOSE])
    s2 = await _investigate(_graph(w, llm2, pool), second)
    assert [r["alert_id"] for r in s2["history"]] == [first]
    prompts = " ".join(p for _, p in llm2.prompts)
    assert "payroll pattern, filed SAR" in prompts  # plan and assess both saw it
    assert all("PRIOR HISTORY" in p for name, p in llm2.prompts if name != "SarDraft")
    assert s2["__interrupt__"]  # paused for review, not auto-closed
    assert "prior analyst-approved escalation" in s2["escalation_reason"]


async def test_prior_rejection_does_not_block_auto_close(
    w: World, pool: AsyncConnectionPool
) -> None:
    acct = w.account()
    first, second = _alert_on(w, acct), _alert_on(w, acct, week=1)
    await _investigate(
        _graph(w, ScriptedLLM([ESCALATE]), pool), first, "reject", "false alarm"
    )

    llm = ScriptedLLM([CLOSE])
    s2 = await _investigate(_graph(w, llm, pool), second)
    assert s2["outcome"] == "closed" and not s2.get("__interrupt__")
    assert s2["history"][0]["decision"] == "reject"
    assert "false alarm" in " ".join(p for _, p in llm.prompts)


async def test_other_customers_history_is_not_recalled(
    w: World, pool: AsyncConnectionPool
) -> None:
    a, b = w.account(), w.account()
    first, other = _alert_on(w, a), _alert_on(w, b)
    await _investigate(_graph(w, ScriptedLLM([CLOSE]), pool), first)
    s = await _investigate(_graph(w, ScriptedLLM([CLOSE]), pool), other)
    assert s["history"] == []


async def test_memory_is_idempotent_durable_and_has_no_ground_truth(
    w: World, pool: AsyncConnectionPool
) -> None:
    acct = w.account()
    alert = _alert_on(w, acct)
    graph = _graph(w, ScriptedLLM([CLOSE, CLOSE]), pool)
    await _investigate(graph, alert)
    # Re-running the finished thread (redelivery) must not add a second record.
    await graph.ainvoke(None, {"configurable": {"thread_id": f"alert-{alert}"}})

    items = await _store(pool).asearch(("customer_history",), limit=50)
    assert len(items) == 1 and items[0].key == f"alert-{alert}"
    blob = json.dumps([i.value for i in items])
    assert SECRET_TYPOLOGY not in blob and "is_true_positive" not in blob

    # A brand-new store object (e.g. worker restart) reads the same record back.
    fresh = await _store(pool).asearch(("customer_history",), limit=50)
    assert [i.value["alert_id"] for i in fresh] == [alert]

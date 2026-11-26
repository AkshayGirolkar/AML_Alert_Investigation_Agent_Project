"""F12: SqlUsageRecorder against real Postgres (skips when the DB is unreachable)."""

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.agent.gateway import CallRecord
from app.agent.usage_store import SqlUsageRecorder
from app.config import get_settings


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(get_settings().async_database_url)
    try:
        async with eng.connect() as c:
            await c.execute(text("SELECT 1 FROM llm_calls LIMIT 1"))
    except (SQLAlchemyError, OSError):
        await eng.dispose()
        pytest.skip("Postgres with migration 0008 is not available")
    yield eng
    await eng.dispose()


def _rec(case_id: int, seq: int, cost: float) -> CallRecord:
    return CallRecord(
        case_id, "assess", seq, "openai", "gpt-4o", 10, 5, cost, 120, 1, False, None
    )


async def test_record_is_idempotent_and_load_sums_cost(engine: AsyncEngine) -> None:
    async with engine.begin() as c:  # a throwaway case row for the FK
        await c.execute(text("DELETE FROM llm_calls WHERE case_id = 999001"))
        await c.execute(text("DELETE FROM cases WHERE case_id = 999001"))
        alert = (await c.execute(text("SELECT alert_id FROM alerts LIMIT 1"))).scalar()
        if alert is None:
            pytest.skip("no alerts loaded")
        await c.execute(
            text(
                "INSERT INTO cases (case_id, alert_id, status) VALUES (999001, :a, 'queued')"
            ),
            {"a": alert},
        )
    rec = SqlUsageRecorder(engine)
    try:
        assert await rec.load(999001) == (0.0, 1)
        await rec.record(_rec(999001, 1, 0.25))
        await rec.record(_rec(999001, 1, 0.25))  # replay: ignored
        await rec.record(_rec(999001, 2, 0.5))
        assert await rec.load(999001) == (0.75, 3)
        async with engine.connect() as c:
            row = (
                await c.execute(
                    text(
                        "SELECT calls, cost_usd FROM llm_case_costs WHERE case_id = 999001"
                    )
                )
            ).one()
        assert row[0] == 2 and float(row[1]) == 0.75
    finally:
        async with engine.begin() as c:
            await c.execute(text("DELETE FROM llm_calls WHERE case_id = 999001"))
            await c.execute(text("DELETE FROM cases WHERE case_id = 999001"))

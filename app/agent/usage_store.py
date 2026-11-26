"""Postgres recorder for LLM usage (F12). Insert is idempotent on (case_id, call_seq)."""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.agent.gateway import CallRecord

INSERT = text(
    "INSERT INTO llm_calls (case_id, call_seq, node, provider, model, input_tokens, "
    "output_tokens, cost_usd, latency_ms, attempt, fallback, error) VALUES "
    "(:case_id, :call_seq, :node, :provider, :model, :input_tokens, :output_tokens, "
    ":cost_usd, :latency_ms, :attempt, :fallback, :error) "
    "ON CONFLICT (case_id, call_seq) DO NOTHING"
)
LOAD = text(
    "SELECT coalesce(sum(cost_usd), 0), coalesce(max(call_seq), 0) + 1 "
    "FROM llm_calls WHERE case_id = :case_id"
)


class SqlUsageRecorder:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def record(self, rec: CallRecord) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(INSERT, rec.__dict__)

    async def load(self, case_id: int) -> tuple[float, int]:
        async with self._engine.connect() as conn:
            spent, seq = (await conn.execute(LOAD, {"case_id": case_id})).one()
        return float(spent), int(seq)

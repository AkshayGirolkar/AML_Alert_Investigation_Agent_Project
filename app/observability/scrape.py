"""Scrape-time gauges (F13): read durable state when /metrics is requested.

Every source is optional and failure-tolerant: a missing DB or SQS just drops those
series, it never fails the scrape.
"""

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import text

from app.observability.metrics import metrics, render_gauges

log = logging.getLogger("metrics")

Gauge = tuple[str, dict[str, str], float]


async def db_gauges(engine: Any) -> list[Gauge]:
    out: list[Gauge] = []
    async with engine.connect() as conn:
        rows = await conn.execute(
            text("SELECT status, count(*) FROM cases GROUP BY status")
        )
        out += [("cases", {"status": s}, float(n)) for s, n in rows]
        row = (
            await conn.execute(
                text(
                    "SELECT coalesce(sum(cost_usd), 0), coalesce(sum(input_tokens), 0),"
                    " coalesce(sum(output_tokens), 0), count(*) FROM llm_calls"
                )
            )
        ).one()
        out += [
            ("llm_recorded_cost_usd", {}, float(row[0])),
            ("llm_recorded_input_tokens", {}, float(row[1])),
            ("llm_recorded_output_tokens", {}, float(row[2])),
            ("llm_recorded_calls", {}, float(row[3])),
        ]
        age = (
            await conn.execute(
                text(
                    "SELECT coalesce(extract(epoch FROM now() - min(updated_at)), 0)"
                    " FROM cases WHERE status = 'awaiting_review'"
                )
            )
        ).scalar_one()
        out.append(("review_backlog_oldest_age_seconds", {}, float(age)))
    return out


async def scrape(
    engine: Any, depths: Callable[[], Awaitable[tuple[int, int]]] | None
) -> str:
    gauges: list[Gauge] = []
    if engine is not None:
        try:
            gauges += await db_gauges(engine)
        except Exception:
            log.exception("metrics: database gauges unavailable")
    if depths is not None:
        try:
            queue, dlq = await depths()
            gauges += [("queue_depth", {}, float(queue)), ("dlq_depth", {}, float(dlq))]
        except Exception:
            log.exception("metrics: queue depth unavailable")
    return metrics.render() + render_gauges(gauges)

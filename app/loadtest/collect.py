"""Read a finished (or running) load test back from the database (F16).

All times come from the database clock (`now()` defaults), so differences between
`cases.created_at`, `llm_calls.created_at` and `cases.updated_at` are consistent.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable, Sequence

import psycopg

from app.db.copy import conninfo
from app.loadtest.metrics import FINISHED, CaseRow

CASES_SQL = """
SELECT c.alert_id, c.status,
       extract(epoch FROM c.created_at)::float8,
       extract(epoch FROM c.updated_at)::float8,
       c.attempts,
       coalesce(u.plan_ok, 0), coalesce(d.n, 0),
       coalesce(u.cost, 0)::float8, coalesce(u.inp, 0), coalesce(u.outp, 0),
       u.first_start
FROM cases c
LEFT JOIN (
    SELECT case_id,
           count(*) FILTER (WHERE node = 'plan' AND error IS NULL) AS plan_ok,
           sum(cost_usd) AS cost,
           sum(input_tokens) AS inp, sum(output_tokens) AS outp,
           min(extract(epoch FROM created_at) - latency_ms / 1000.0)::float8
               AS first_start
    FROM llm_calls GROUP BY case_id
) u ON u.case_id = c.case_id
LEFT JOIN (SELECT case_id, count(*) AS n FROM decisions GROUP BY case_id) d
       ON d.case_id = c.case_id
WHERE c.alert_id = ANY(%s)
ORDER BY c.alert_id
"""


def fetch_case_rows(database_url: str, alert_ids: Sequence[int]) -> list[CaseRow]:
    with psycopg.connect(conninfo(database_url)) as conn:
        rows = conn.execute(CASES_SQL, (list(alert_ids),)).fetchall()
    out: list[CaseRow] = []
    for (
        a,
        status,
        created,
        updated,
        attempts,
        plan_ok,
        dec,
        cost,
        inp,
        outp,
        start,
    ) in rows:
        final = status in FINISHED
        out.append(
            CaseRow(
                alert_id=int(a),
                status=status,
                enqueued_at=created,
                started_at=start,
                finished_at=updated if final else None,
                attempts=int(attempts),
                plan_calls_ok=int(plan_ok),
                decision_count=int(dec),
                cost_usd=cost,
                input_tokens=int(inp),
                output_tokens=int(outp),
            )
        )
    return out


async def wait_for_drain(
    unfinished: Callable[[], Awaitable[int]],
    timeout: float,
    poll: float = 2.0,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    clock: Callable[[], float] = time.monotonic,
    abort: Callable[[], Awaitable[bool]] | None = None,
) -> str:
    """Poll until nothing is unfinished. Returns "drained", "timeout" or "aborted"."""
    deadline = clock() + timeout
    while True:
        if await unfinished() == 0:
            return "drained"
        if abort is not None and await abort():
            return "aborted"
        if clock() >= deadline:
            return "timeout"
        await sleep(poll)


def count_unfinished(database_url: str, alert_ids: Sequence[int]) -> int:
    """Cases not yet final, plus alerts that have no case row yet."""
    with psycopg.connect(conninfo(database_url)) as conn:
        row = conn.execute(
            "SELECT count(*) FROM unnest(%s::bigint[]) AS a(id) "
            "LEFT JOIN cases c ON c.alert_id = a.id "
            "WHERE c.case_id IS NULL OR c.status <> ALL(%s)",
            (list(alert_ids), sorted(FINISHED)),
        ).fetchone()
    return int(row[0]) if row else 0


def total_cost(database_url: str, alert_ids: Sequence[int]) -> float:
    with psycopg.connect(conninfo(database_url)) as conn:
        row = conn.execute(
            "SELECT coalesce(sum(l.cost_usd), 0)::float8 FROM llm_calls l "
            "JOIN cases c ON c.case_id = l.case_id WHERE c.alert_id = ANY(%s)",
            (list(alert_ids),),
        ).fetchone()
    return float(row[0]) if row else 0.0

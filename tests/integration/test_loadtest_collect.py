"""F16: reading a load run back from Postgres (`aml_test` database)."""

from datetime import datetime, timedelta

from app.loadtest.collect import count_unfinished, fetch_case_rows, total_cost
from app.loadtest.metrics import compute_run_metrics
from tests.integration.test_loadtest_seed import _alerts
from tests.integration.test_rules import World, _conn, _db, w  # noqa: F401

T0 = datetime(2026, 1, 1, 12, 0, 0)


def _case(
    url: str, alert_id: int, status: str, created: int, updated: int, attempts: int = 1
) -> int:
    with _conn(url) as c:
        row = c.execute(
            "INSERT INTO cases (alert_id, status, attempts, created_at, updated_at) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING case_id",
            (
                alert_id,
                status,
                attempts,
                T0 + timedelta(seconds=created),
                T0 + timedelta(seconds=updated),
            ),
        ).fetchone()
        assert row is not None
        return int(row[0])


def _call(
    url: str, case_id: int, seq: int, node: str, end: int, ms: int, **kw: object
) -> None:
    with _conn(url) as c:
        c.execute(
            "INSERT INTO llm_calls (case_id, call_seq, node, provider, model, "
            "input_tokens, output_tokens, cost_usd, latency_ms, attempt, fallback, "
            "error, created_at) VALUES (%s, %s, %s, 'synthetic', 'synthetic-llm', "
            "100, 10, 0.25, %s, 1, false, %s, %s)",
            (case_id, seq, node, ms, kw.get("error"), T0 + timedelta(seconds=end)),
        )


def test_rows_metrics_and_unfinished(w: World) -> None:  # noqa: F811
    a, b, c = _alerts(w, 3)
    done = _case(w.url, a, "completed", 0, 30)
    _call(w.url, done, 1, "plan", end=12, ms=2000)  # call started at t=10
    _call(w.url, done, 2, "assess", end=14, ms=2000)
    redo = _case(w.url, b, "awaiting_review", 0, 50, attempts=2)
    _call(w.url, redo, 1, "plan", end=22, ms=2000)
    _call(w.url, redo, 2, "plan", end=24, ms=2000)  # ran twice: a duplicate
    _case(w.url, c, "running", 0, 5)

    rows = {r.alert_id: r for r in fetch_case_rows(w.url, [a, b, c])}
    start, fin = rows[a].started_at, rows[a].finished_at
    assert start is not None and fin is not None
    assert start - rows[a].enqueued_at == 10
    assert fin - rows[a].enqueued_at == 30
    assert (rows[a].plan_calls_ok, rows[a].cost_usd, rows[a].input_tokens) == (
        1,
        0.5,
        200,
    )
    assert rows[b].attempts == 2 and rows[b].plan_calls_ok == 2
    assert rows[c].finished_at is None  # running: not final

    m = compute_run_metrics(list(rows.values()))
    assert m.failures.duplicates == 1 and m.failures.redeliveries == 1
    assert m.failures.unfinished == 1 and m.n_finished == 2
    assert count_unfinished(w.url, [a, b, c]) == 1
    assert total_cost(w.url, [a, b, c]) == 1.0


def test_alert_without_a_case_counts_as_unfinished(w: World) -> None:  # noqa: F811
    (a,) = _alerts(w, 1)
    assert count_unfinished(w.url, [a]) == 1
    assert fetch_case_rows(w.url, [a]) == []


def test_failed_plan_call_is_not_a_successful_plan(w: World) -> None:  # noqa: F811
    (a,) = _alerts(w, 1)
    case = _case(w.url, a, "completed", 0, 9)
    _call(w.url, case, 1, "plan", end=3, ms=100, error="TimeoutError()")
    _call(w.url, case, 2, "plan", end=6, ms=100)
    (row,) = fetch_case_rows(w.url, [a])
    assert row.plan_calls_ok == 1

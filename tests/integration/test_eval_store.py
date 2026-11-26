"""F15: eval tables (migration 0009) and persistence against the `aml_test` database."""

from dataclasses import replace
from pathlib import Path

from app.eval.metrics import ResultRow
from app.eval.store import (
    RunInfo,
    finish_run,
    load_info,
    load_results,
    read_run_file,
    save_result,
    save_run,
    write_run_file,
)
from tests.integration.test_rules import _conn, _db  # noqa: F401

INFO = RunInfo("run-a", "ds", "gpt-4o-mini", "gpt-4o", "abc123", {"concurrency": 4})
ROW = ResultRow(
    7,
    "fan_in",
    True,
    "CYCLE",
    outcome="escalated",
    typology="cycle",
    risk=0.9,
    confidence=0.8,
    grounded=True,
    n_citations=2,
    tool_calls=3,
    cost_usd=0.04,
    seconds=12.5,
    rationale="because",
    node_costs={"plan": 0.01, "assess": 0.03},
)


def test_roundtrip_upsert_and_no_case_rows(_db: str) -> None:  # noqa: F811
    with _conn(_db) as c:
        c.execute("DELETE FROM eval_runs")
    save_run(_db, INFO)
    save_run(_db, INFO)  # idempotent
    save_result(_db, "run-a", ROW)
    save_result(_db, "run-a", replace(ROW, outcome="closed"))  # upsert replaces
    save_result(_db, "run-a", replace(ROW, alert_id=8, error="boom", outcome=None))
    finish_run(_db, "run-a")

    rows = load_results(_db, "run-a")
    assert [r.alert_id for r in rows] == [7, 8]
    assert rows[0] == replace(ROW, outcome="closed")
    assert rows[1].error == "boom"
    assert load_info(_db, "run-a") == INFO
    assert load_info(_db, "nope") is None
    with _conn(_db) as c:
        done = c.execute("SELECT finished_at FROM eval_runs").fetchone()
    assert done is not None and done[0] is not None


def test_run_file_roundtrip(tmp_path: Path) -> None:
    p = tmp_path / "runs" / "r.json"
    write_run_file(p, INFO, [ROW])
    info, rows = read_run_file(p)
    assert info == INFO and rows == [ROW]

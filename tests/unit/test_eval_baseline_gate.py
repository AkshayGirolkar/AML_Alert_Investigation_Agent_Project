import json
from pathlib import Path

import pytest

from app.eval.baseline import baseline_metrics, baseline_rows, compare
from app.eval.gate import Thresholds, check_gate
from app.eval.metrics import ResultRow, compute_metrics


def r(i: int, truth: bool, outcome: str | None, typ: str | None = None, **kw: object):
    return ResultRow(
        alert_id=i,
        rule_id="fan_out",
        truth=truth,
        truth_typology=typ,
        outcome=outcome,
        typology=typ if outcome == "escalated" else None,
        grounded=True,
        n_citations=1,
        **kw,  # type: ignore[arg-type]
    )


# 4 true (3 caught, 1 missed) + 6 false (1 escalated, 5 closed)
ROWS = [
    r(1, True, "escalated", "CYCLE"),
    r(2, True, "escalated", "CYCLE"),
    r(3, True, "escalated", "FAN-IN"),
    r(4, True, "closed", "FAN-IN"),
    r(5, False, "escalated"),
    *[r(i, False, "closed") for i in range(6, 11)],
]


def test_baseline_escalates_everything_with_no_llm() -> None:
    rows = baseline_rows(ROWS)
    assert all(x.escalated for x in rows)
    assert all(x.cost_usd == 0 and x.typology is None for x in rows)
    b = baseline_metrics(ROWS).overall
    assert b.recall == 1.0
    assert b.precision == pytest.approx(0.4)  # 4 true of 10 alerts
    assert b.typology_accuracy == 0.0


def test_comparison() -> None:
    c = compare(compute_metrics(ROWS), baseline_metrics(ROWS))
    assert c.recall_lost == pytest.approx(0.25)
    assert c.precision_gain == pytest.approx(0.75 - 0.4)
    assert c.escalations_avoided == 6  # 10 baseline - 4 agent
    assert c.workload_reduction == pytest.approx(0.6)
    assert c.false_alerts_closed == 5 and c.true_alerts_missed == 1


def test_gate_passes_and_fails_with_reasons() -> None:
    m = compute_metrics(ROWS)
    ok = check_gate(m, Thresholds(min_recall=0.7, min_grounding_rate=0.9))
    assert ok.passed and ok.failures == []

    bad = check_gate(
        m,
        Thresholds(min_recall=0.9, min_grounding_rate=1.1, max_mean_cost_usd=-1.0),
    )
    assert not bad.passed
    text = " | ".join(bad.failures)
    assert "recall 0.750" in text and "grounding rate" in text
    assert "error rate" not in text  # no errors in ROWS
    assert any("mean cost" in f for f in bad.failures)  # cost 0.0 > max -1.0


def test_gate_per_typology_ignores_small_groups() -> None:
    m = compute_metrics(ROWS)  # CYCLE n=2, FAN-IN n=2 (one missed)
    t = Thresholds(min_typology_recall=0.9, min_typology_group_size=5)
    assert check_gate(m, t).passed
    t2 = Thresholds(min_typology_recall=0.9, min_typology_group_size=2)
    res = check_gate(m, t2)
    assert not res.passed and any("FAN-IN" in f for f in res.failures)


def test_gate_undefined_metrics_fail_closed() -> None:
    m = compute_metrics([r(1, False, "closed")])  # no true alerts
    res = check_gate(m, Thresholds())
    assert not res.passed and "no true alerts" in res.failures[0]


def test_error_rate_and_loading(tmp_path: Path) -> None:
    rows = [*ROWS, r(99, True, None, "CYCLE", error="boom")]
    p = tmp_path / "t.json"
    p.write_text(json.dumps({"min_recall": 0.1, "max_error_rate": 0.05}))
    res = check_gate(compute_metrics(rows), Thresholds.load(p))
    assert not res.passed and any("error rate" in f for f in res.failures)


def test_committed_fixture_passes_committed_thresholds() -> None:
    """The replay gate CI will run: guards the fixture and thresholds against drift."""
    from app.eval.store import read_run_file

    root = Path(__file__).parent.parent.parent / "eval"
    _, rows = read_run_file(root / "fixtures" / "recorded_run.json")
    res = check_gate(compute_metrics(rows), Thresholds.load(root / "thresholds.json"))
    assert res.passed, res.failures

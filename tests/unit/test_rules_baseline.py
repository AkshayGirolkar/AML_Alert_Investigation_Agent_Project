"""Unit tests for F04 baseline metric math and typology labeling (no DB)."""

import pytest
from app.rules.baseline import BaselineReport, Metrics, compute_metrics, format_report
from app.rules.labeling import pick_typology


def test_metrics_basic() -> None:
    m = compute_metrics(alerts=10, true_positive_alerts=2, covered=3, total=4)
    assert m.alerts == 10
    assert m.precision == pytest.approx(0.2)
    assert m.fp_rate == pytest.approx(0.8)
    assert m.recall == pytest.approx(0.75)


def test_metrics_no_alerts_gives_none_not_zero_division() -> None:
    m = compute_metrics(alerts=0, true_positive_alerts=0, covered=0, total=5)
    assert m.precision is None and m.fp_rate is None
    assert m.recall == 0.0


def test_metrics_no_ground_truth_gives_none_recall() -> None:
    m = compute_metrics(alerts=3, true_positive_alerts=0, covered=0, total=0)
    assert m.recall is None
    assert m.precision == 0.0


def test_metrics_rejects_impossible_counts() -> None:
    with pytest.raises(ValueError):
        compute_metrics(alerts=1, true_positive_alerts=2, covered=0, total=1)
    with pytest.raises(ValueError):
        compute_metrics(alerts=1, true_positive_alerts=0, covered=2, total=1)


def test_pick_typology_most_frequent_then_alphabetical() -> None:
    assert pick_typology([]) is None
    assert pick_typology([None, None]) is None
    assert pick_typology(["CYCLE", None]) == "CYCLE"
    assert pick_typology(["FAN-OUT", "CYCLE", "CYCLE"]) == "CYCLE"
    assert pick_typology(["FAN-OUT", "CYCLE"]) == "CYCLE"  # tie -> alphabetical


def test_format_report_mentions_every_rule_typology_and_untyped() -> None:
    zero = compute_metrics(alerts=0, true_positive_alerts=0, covered=0, total=0)
    report = BaselineReport(
        overall=Metrics(10, 2, 0.2, 0.8, 0.5),
        by_rule={"structuring": Metrics(4, 1, 0.25, 0.75, 0.1)},
        by_typology={"CYCLE": zero, "untyped": zero},
        attempt_recall=0.5,
    )
    text = format_report(report)
    for needle in ("structuring", "CYCLE", "untyped", "precision", "recall"):
        assert needle in text

import pytest

from app.eval.metrics import (
    ResultRow,
    compute_metrics,
    percentile,
    wilson_interval,
)
from app.eval.typology import normalize_typology


def row(
    alert_id: int,
    truth: bool,
    outcome: str | None,
    *,
    truth_typology: str | None = None,
    typology: str | None = None,
    rule_id: str = "high_velocity",
    grounded: bool = True,
    n_citations: int = 2,
    **kw: object,
) -> ResultRow:
    return ResultRow(
        alert_id=alert_id,
        rule_id=rule_id,
        truth=truth,
        truth_typology=truth_typology,
        outcome=outcome,
        typology=typology,
        grounded=grounded,
        n_citations=n_citations,
        **kw,  # type: ignore[arg-type]
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("fan-in", "FAN-IN"),
        ("Fan In", "FAN-IN"),
        ("fan_out", "FAN-OUT"),
        ("FANOUT", "FAN-OUT"),
        ("scatter gather", "SCATTER-GATHER"),
        ("round-tripping", "CYCLE"),
        ("STACK", "STACK"),
        ("money mule", None),
        ("", None),
        (None, None),
    ],
)
def test_normalize_typology(raw: str | None, expected: str | None) -> None:
    assert normalize_typology(raw) == expected


def test_wilson_edges() -> None:
    assert wilson_interval(0, 0) is None
    lo, hi = wilson_interval(0, 10)  # type: ignore[misc]
    assert lo == 0.0 and 0.2 < hi < 0.4
    lo, hi = wilson_interval(10, 10)  # type: ignore[misc]
    assert hi == 1.0 and 0.6 < lo < 0.8


def test_percentile() -> None:
    assert percentile([], 50) is None
    assert percentile([3.0], 95) == 3.0
    assert percentile([1.0, 2.0, 3.0, 4.0], 50) == 2.5
    assert percentile([1.0, 2.0, 3.0, 4.0], 100) == 4.0


def test_core_rates() -> None:
    rows = [
        row(1, True, "escalated", truth_typology="CYCLE", typology="round trip"),
        row(2, True, "escalated", truth_typology="CYCLE", typology="stack"),
        row(3, True, "closed", truth_typology="FAN-IN"),
        row(4, False, "escalated"),
        row(5, False, "closed"),
        row(6, False, "closed"),
    ]
    m = compute_metrics(rows).overall
    assert m.true_alerts == 3 and m.caught == 2
    assert m.recall == pytest.approx(2 / 3)
    assert m.false_closure_rate == pytest.approx(1 / 3)
    assert m.precision == pytest.approx(2 / 3)  # 2 caught of 3 escalated
    assert m.false_alert_closure_rate == pytest.approx(2 / 3)
    assert m.typology_accuracy == pytest.approx(1 / 2)  # CYCLE hit, STACK != CYCLE


def test_per_typology_and_zero_cases() -> None:
    rows = [
        row(1, True, "closed", truth_typology="CYCLE"),
        row(2, False, "closed"),
    ]
    em = compute_metrics(rows)
    assert set(em.by_typology) == {"CYCLE"}
    assert em.by_typology["CYCLE"].recall == 0.0
    assert em.overall.precision is None  # nothing escalated: undefined, not 0
    assert em.by_typology["CYCLE"].typology_accuracy is None  # nothing caught


def test_no_true_alerts() -> None:
    m = compute_metrics([row(1, False, "escalated")]).overall
    assert m.recall is None and m.false_closure_rate is None
    assert m.precision == 0.0


def test_errors_and_budget_are_misses() -> None:
    rows = [
        row(1, True, None, truth_typology="CYCLE", error="boom"),
        row(2, True, "escalated", truth_typology="CYCLE", budget_exceeded=True),
    ]
    em = compute_metrics(rows)
    assert em.overall.caught == 0 and em.overall.recall == 0.0
    assert em.overall.escalated == 0
    assert em.error_rate == 0.5


def test_all_error_run() -> None:
    rows = [row(i, i % 2 == 0, None, error="x") for i in range(4)]
    em = compute_metrics(rows)
    assert em.error_rate == 1.0
    assert em.grounding_rate is None and em.mean_tool_calls is None


def test_grounding_rate_needs_a_citation() -> None:
    rows = [
        row(1, False, "closed"),
        row(2, False, "closed", n_citations=0),
        row(3, False, "closed", grounded=False),
        row(4, False, None, error="x"),  # errors are outside the denominator
    ]
    assert compute_metrics(rows).grounding_rate == pytest.approx(1 / 3)


def test_cost_latency_and_unmapped() -> None:
    rows = [
        row(
            1,
            True,
            "escalated",
            truth_typology="CYCLE",
            typology="money mule",
            cost_usd=0.02,
            seconds=10.0,
        ),
        row(2, False, "closed", cost_usd=0.04, seconds=20.0),
    ]
    em = compute_metrics(rows)
    assert em.cost.mean == pytest.approx(0.03)
    assert em.seconds.p50 == 15.0
    assert em.unmapped_typologies == {"money mule": 1}


def test_untyped_true_alerts_get_their_own_group() -> None:
    rows = [
        row(1, True, "escalated", truth_typology=None),
        row(2, True, "closed", truth_typology=None),
        row(3, True, "escalated", truth_typology="CYCLE", typology="cycle"),
    ]
    em = compute_metrics(rows)
    assert set(em.by_typology) == {"CYCLE", "UNTYPED"}
    assert em.by_typology["UNTYPED"].recall == 0.5
    assert em.by_typology["UNTYPED"].typology_accuracy is None  # no name to match


def test_typology_named_rate_separates_silence_from_wrong_names() -> None:
    rows = [
        row(1, True, "escalated", truth_typology="CYCLE", typology=None),
        row(2, True, "escalated", truth_typology="CYCLE", typology="cycle"),
        row(3, True, "escalated", truth_typology="STACK", typology="cycle"),
        row(4, True, "escalated", truth_typology="STACK", typology="money mule"),
    ]
    g = compute_metrics(rows).overall
    assert g.typology_accuracy == 0.25  # 1 of 4
    assert g.typology_named_rate == 0.5  # 2 of 4 gave a recognised name
    assert g.typology_accuracy_when_named == 0.5  # 1 of those 2

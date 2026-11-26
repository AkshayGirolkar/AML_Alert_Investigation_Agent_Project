"""Rules-only baseline on the same alert set (F15).

The F04 rules produce an alert queue and an analyst works every alert, so the rules-only
baseline is "escalate every alert". Recall on rule-covered true alerts is 1.0 by
construction; the comparison is about precision and analyst workload.
"""

from collections.abc import Sequence
from dataclasses import dataclass, replace

from app.eval.metrics import (
    EvalMetrics,
    GroupMetrics,
    ResultRow,
    compute_metrics,
    ratio,
)


def baseline_rows(rows: Sequence[ResultRow]) -> list[ResultRow]:
    """The same alerts, every one escalated, with no typology and no LLM cost."""
    return [
        replace(
            r,
            outcome="escalated",
            typology=None,
            risk=None,
            confidence=None,
            grounded=False,
            n_citations=0,
            tool_calls=0,
            cost_usd=0.0,
            seconds=0.0,
            error=None,
            budget_exceeded=False,
            rationale="",
        )
        for r in rows
    ]


def baseline_metrics(rows: Sequence[ResultRow]) -> EvalMetrics:
    return compute_metrics(baseline_rows(rows))


@dataclass(frozen=True)
class Comparison:
    agent: GroupMetrics
    baseline: GroupMetrics
    recall_lost: float | None  # baseline recall - agent recall
    precision_gain: float | None  # agent precision - baseline precision
    escalations_avoided: int  # alerts the analyst no longer has to work
    workload_reduction: float | None  # escalations_avoided / baseline escalations
    false_alerts_closed: int  # false-positive alerts the agent closed
    true_alerts_missed: int  # true alerts the agent did not escalate


def compare(agent: EvalMetrics, baseline: EvalMetrics) -> Comparison:
    a, b = agent.overall, baseline.overall
    avoided = b.escalated - a.escalated
    return Comparison(
        agent=a,
        baseline=b,
        recall_lost=(
            b.recall - a.recall
            if a.recall is not None and b.recall is not None
            else None
        ),
        precision_gain=(
            a.precision - b.precision
            if a.precision is not None and b.precision is not None
            else None
        ),
        escalations_avoided=avoided,
        workload_reduction=ratio(avoided, b.escalated),
        false_alerts_closed=round((a.false_alert_closure_rate or 0.0) * a.false_alerts),
        true_alerts_missed=a.true_alerts - a.caught,
    )

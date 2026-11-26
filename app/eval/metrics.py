"""Pure scoring maths for the evaluation harness (F15). No DB, no LLM.

A result row is one alert scored against its ground truth. A true alert counts as caught
only if the agent produced a verdict and escalated; errors and over-budget cases are
misses (a missed launderer is the costly error).
"""

import math
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from app.eval.typology import UNTYPED, normalize_typology


@dataclass(frozen=True)
class ResultRow:
    alert_id: int
    rule_id: str
    truth: bool  # alerts.is_true_positive
    truth_typology: str | None  # alerts.typology (IBM name) for true alerts
    outcome: str | None = None  # "closed" | "escalated" | None when the run failed
    typology: str | None = None  # agent's free-text typology
    risk: float | None = None
    confidence: float | None = None
    grounded: bool = False
    n_citations: int = 0  # evidence ids cited by the verdict
    tool_calls: int = 0
    cost_usd: float = 0.0
    seconds: float = 0.0
    error: str | None = None
    budget_exceeded: bool = False
    rationale: str = ""
    node_costs: dict[str, float] = field(default_factory=dict)  # USD per graph node

    @property
    def escalated(self) -> bool:
        return (
            self.error is None
            and not self.budget_exceeded
            and self.outcome == "escalated"
        )

    @property
    def closed(self) -> bool:
        return (
            self.error is None and not self.budget_exceeded and self.outcome == "closed"
        )


def ratio(num: float, den: float) -> float | None:
    """num / den, or None when undefined (no denominator)."""
    return num / den if den else None


def wilson_interval(
    successes: int, n: int, z: float = 1.96
) -> tuple[float, float] | None:
    """95% Wilson score interval for a proportion; None when n == 0."""
    if n <= 0:
        return None
    p = successes / n
    z2 = z * z
    centre = (p + z2 / (2 * n)) / (1 + z2 / n)
    half = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n)) / (1 + z2 / n)
    return max(0.0, centre - half), min(1.0, centre + half)


def percentile(values: Sequence[float], q: float) -> float | None:
    """Linear-interpolated percentile (q in 0..100); None for an empty list."""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    pos = (len(ordered) - 1) * q / 100
    lo = math.floor(pos)
    hi = math.ceil(pos)
    return float(ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo))


@dataclass(frozen=True)
class Stat:
    mean: float | None
    p50: float | None
    p95: float | None


def summarize(values: Sequence[float]) -> Stat:
    return Stat(
        sum(values) / len(values) if values else None,
        percentile(values, 50),
        percentile(values, 95),
    )


@dataclass(frozen=True)
class GroupMetrics:
    n: int
    true_alerts: int
    false_alerts: int
    escalated: int
    caught: int  # true alerts escalated
    recall: float | None
    recall_ci: tuple[float, float] | None
    false_closure_rate: float | None  # true alerts closed or not scored / true alerts
    precision: float | None  # caught / escalated
    precision_ci: tuple[float, float] | None
    false_alert_closure_rate: float | None  # false alerts closed / false alerts
    typology_accuracy: float | None  # among caught true alerts that have a typology
    typology_named_rate: float | None  # of those, share where the agent named a pattern
    typology_accuracy_when_named: float | None  # of those named, share that match


@dataclass(frozen=True)
class EvalMetrics:
    overall: GroupMetrics
    by_typology: dict[str, GroupMetrics]  # grouped by true typology (true alerts only)
    by_rule: dict[str, GroupMetrics]
    error_rate: float | None
    grounding_rate: float | None  # grounded verdicts citing >= 1 id / non-error rows
    cost: Stat
    seconds: Stat
    mean_tool_calls: float | None
    unmapped_typologies: dict[str, int] = field(default_factory=dict)


def group_metrics(rows: Sequence[ResultRow]) -> GroupMetrics:
    true_rows = [r for r in rows if r.truth]
    false_rows = [r for r in rows if not r.truth]
    escalated = [r for r in rows if r.escalated]
    caught = [r for r in true_rows if r.escalated]
    named = [r for r in caught if r.truth_typology]
    named_by_agent = [r for r in named if normalize_typology(r.typology) is not None]
    hits = sum(
        normalize_typology(r.typology) == normalize_typology(r.truth_typology)
        and normalize_typology(r.typology) is not None
        for r in named
    )
    return GroupMetrics(
        n=len(rows),
        true_alerts=len(true_rows),
        false_alerts=len(false_rows),
        escalated=len(escalated),
        caught=len(caught),
        recall=ratio(len(caught), len(true_rows)),
        recall_ci=wilson_interval(len(caught), len(true_rows)),
        false_closure_rate=ratio(len(true_rows) - len(caught), len(true_rows)),
        precision=ratio(len(caught), len(escalated)),
        precision_ci=wilson_interval(len(caught), len(escalated)),
        false_alert_closure_rate=ratio(
            sum(r.closed for r in false_rows), len(false_rows)
        ),
        typology_accuracy=ratio(hits, len(named)),
        typology_named_rate=ratio(len(named_by_agent), len(named)),
        typology_accuracy_when_named=ratio(hits, len(named_by_agent)),
    )


def _group_by(rows: Iterable[ResultRow], key: str) -> dict[str, list[ResultRow]]:
    out: dict[str, list[ResultRow]] = defaultdict(list)
    for r in rows:
        out[getattr(r, key)].append(r)
    return dict(out)


def compute_metrics(rows: Sequence[ResultRow]) -> EvalMetrics:
    true_rows = [r for r in rows if r.truth]
    by_typology: dict[str, GroupMetrics] = {}
    groups: dict[str, list[ResultRow]] = defaultdict(list)
    for r in true_rows:
        groups[r.truth_typology or UNTYPED].append(r)
    for t, g in sorted(groups.items()):
        by_typology[t] = group_metrics(g)
    by_rule = {
        k: group_metrics(g) for k, g in sorted(_group_by(rows, "rule_id").items())
    }
    scored = [r for r in rows if r.error is None]
    grounded = sum(r.grounded and r.n_citations >= 1 for r in scored)
    unmapped: dict[str, int] = defaultdict(int)
    for r in rows:
        if r.escalated and r.typology and normalize_typology(r.typology) is None:
            unmapped[r.typology] += 1
    return EvalMetrics(
        overall=group_metrics(rows),
        by_typology=by_typology,
        by_rule=by_rule,
        error_rate=ratio(len(rows) - len(scored), len(rows)),
        grounding_rate=ratio(grounded, len(scored)),
        cost=summarize([r.cost_usd for r in rows]),
        seconds=summarize([r.seconds for r in rows]),
        mean_tool_calls=ratio(sum(r.tool_calls for r in scored), len(scored)),
        unmapped_typologies=dict(sorted(unmapped.items())),
    )


def typology_confusion(rows: Sequence[ResultRow]) -> dict[str, dict[str, int]]:
    """True typology -> {named typology or "(none)": count}, escalated true alerts only.

    Shows where the evidence cannot separate patterns (F19): a row spread over many
    columns means the agent is guessing.
    """
    out: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for r in rows:
        if r.truth and r.escalated:
            named = normalize_typology(r.typology) or "(none)"
            out[r.truth_typology or UNTYPED][named] += 1
    return {k: dict(sorted(v.items())) for k, v in sorted(out.items())}

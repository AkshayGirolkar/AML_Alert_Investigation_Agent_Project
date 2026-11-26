"""Eval gate (F15): fail when the agent's metrics breach committed thresholds."""

import json
from dataclasses import dataclass, field
from pathlib import Path

from app.eval.metrics import EvalMetrics


@dataclass(frozen=True)
class Thresholds:
    min_recall: float = 0.0
    min_typology_recall: float = 0.0  # applied only to typologies with enough alerts
    min_typology_group_size: int = 5  # true alerts needed before a typology is gated
    min_grounding_rate: float = 0.0
    max_mean_cost_usd: float | None = None
    max_error_rate: float = 1.0

    @classmethod
    def load(cls, path: str | Path) -> "Thresholds":
        return cls(**json.loads(Path(path).read_text()))


@dataclass(frozen=True)
class GateResult:
    passed: bool
    failures: list[str] = field(default_factory=list)


def check_gate(m: EvalMetrics, t: Thresholds) -> GateResult:
    failures: list[str] = []

    recall = m.overall.recall
    if recall is None:
        failures.append("recall is undefined: the eval set has no true alerts")
    elif recall < t.min_recall:
        failures.append(f"recall {recall:.3f} < minimum {t.min_recall:.3f}")

    for typology, g in m.by_typology.items():
        if g.true_alerts < t.min_typology_group_size or g.recall is None:
            continue
        if g.recall < t.min_typology_recall:
            failures.append(
                f"recall for {typology} {g.recall:.3f} < minimum "
                f"{t.min_typology_recall:.3f} (n={g.true_alerts})"
            )

    if m.grounding_rate is None:
        failures.append("grounding rate is undefined: no alert was scored")
    elif m.grounding_rate < t.min_grounding_rate:
        failures.append(
            f"grounding rate {m.grounding_rate:.3f} < minimum {t.min_grounding_rate:.3f}"
        )

    if t.max_mean_cost_usd is not None and (
        m.cost.mean is not None and m.cost.mean > t.max_mean_cost_usd
    ):
        failures.append(
            f"mean cost ${m.cost.mean:.4f} > maximum ${t.max_mean_cost_usd:.4f}"
        )

    if m.error_rate is not None and m.error_rate > t.max_error_rate:
        failures.append(
            f"error rate {m.error_rate:.3f} > maximum {t.max_error_rate:.3f}"
        )

    return GateResult(not failures, failures)

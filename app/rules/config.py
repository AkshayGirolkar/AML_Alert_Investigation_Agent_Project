"""Validated thresholds for the F04 rules engine."""

from dataclasses import dataclass

from app.config import Settings

ALL_RULE_IDS: tuple[str, ...] = (
    "high_velocity",
    "fan_out",
    "fan_in",
    "round_trip",
    "structuring",
    "high_risk_counterparty",
)


def _split(value: str) -> tuple[str, ...]:
    return tuple(p.strip() for p in value.split(",") if p.strip())


@dataclass(frozen=True)
class RuleConfig:
    """All windows are tumbling buckets aligned to the epoch, in hours."""

    velocity_window_hours: int = 1
    velocity_min_txns: int = 10
    fan_window_hours: int = 24
    fan_min_degree: int = 8
    round_trip_window_hours: int = 24
    round_trip_max_hops: int = 3
    structuring_limit: float = 10_000
    structuring_margin: float = 0.10
    structuring_min_txns: int = 3
    structuring_window_hours: int = 24
    high_risk_countries: tuple[str, ...] = ("IR", "KP", "SY", "MM", "CU")
    high_risk_ratings: tuple[str, ...] = ("high",)
    high_risk_window_hours: int = 24
    sanctions_similarity: float = 0.5

    def __post_init__(self) -> None:
        for name in (
            "velocity_window_hours",
            "fan_window_hours",
            "round_trip_window_hours",
            "structuring_window_hours",
            "high_risk_window_hours",
        ):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be >= 1")
        if self.velocity_min_txns < 2:
            raise ValueError("velocity_min_txns must be >= 2")
        if self.fan_min_degree < 2:
            raise ValueError("fan_min_degree must be >= 2")
        if self.structuring_min_txns < 1:
            raise ValueError("structuring_min_txns must be >= 1")
        if self.round_trip_max_hops not in (2, 3):
            raise ValueError("round_trip_max_hops must be 2 or 3")
        if self.structuring_limit <= 0:
            raise ValueError("structuring_limit must be > 0")
        if not 0 < self.structuring_margin < 1:
            raise ValueError("structuring_margin must be between 0 and 1 (exclusive)")
        if not 0 < self.sanctions_similarity <= 1:
            raise ValueError("sanctions_similarity must be in (0, 1]")

    @classmethod
    def from_settings(cls, s: Settings) -> "RuleConfig":
        return cls(
            velocity_window_hours=s.rule_velocity_window_hours,
            velocity_min_txns=s.rule_velocity_min_txns,
            fan_window_hours=s.rule_fan_window_hours,
            fan_min_degree=s.rule_fan_min_degree,
            round_trip_window_hours=s.rule_round_trip_window_hours,
            round_trip_max_hops=s.rule_round_trip_max_hops,
            structuring_limit=s.rule_structuring_limit,
            structuring_margin=s.rule_structuring_margin,
            structuring_min_txns=s.rule_structuring_min_txns,
            structuring_window_hours=s.rule_structuring_window_hours,
            high_risk_countries=_split(s.rule_high_risk_countries),
            high_risk_ratings=_split(s.rule_high_risk_ratings),
            high_risk_window_hours=s.rule_high_risk_window_hours,
            sanctions_similarity=s.rule_sanctions_similarity,
        )

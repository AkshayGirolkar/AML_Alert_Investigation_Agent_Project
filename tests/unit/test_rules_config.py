"""Unit tests for F04 rule thresholds (no DB, no network)."""

import pytest
from app.rules.config import ALL_RULE_IDS, RuleConfig

from app.config import Settings


def test_all_rule_ids_are_the_six_alert_kinds() -> None:
    assert set(ALL_RULE_IDS) == {
        "high_velocity",
        "fan_out",
        "fan_in",
        "round_trip",
        "structuring",
        "high_risk_counterparty",
    }
    assert len(ALL_RULE_IDS) == len(set(ALL_RULE_IDS))


def test_defaults_are_sane() -> None:
    c = RuleConfig()
    assert c.structuring_limit == 10_000
    assert 0 < c.structuring_margin < 1
    assert c.velocity_min_txns >= 2
    assert c.fan_min_degree >= 2
    assert c.round_trip_max_hops in (2, 3)
    assert c.high_risk_ratings == ("high",)


def test_from_settings_reads_overrides() -> None:
    s = Settings(
        postgres_password="x",
        rule_velocity_min_txns=5,
        rule_structuring_limit=5000,
        rule_high_risk_countries="IR, KP ,SY",
        _env_file=None,  # type: ignore[call-arg]
    )
    c = RuleConfig.from_settings(s)
    assert c.velocity_min_txns == 5
    assert c.structuring_limit == 5000
    assert c.high_risk_countries == ("IR", "KP", "SY")


def test_from_settings_without_overrides_equals_defaults() -> None:
    s = Settings(postgres_password="x", _env_file=None)  # type: ignore[call-arg]
    assert RuleConfig.from_settings(s) == RuleConfig()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"structuring_margin": 0.0},
        {"structuring_margin": 1.0},
        {"velocity_min_txns": 1},
        {"fan_min_degree": 1},
        {"round_trip_max_hops": 4},
        {"velocity_window_hours": 0},
        {"structuring_limit": 0},
    ],
)
def test_invalid_thresholds_rejected(kwargs: dict) -> None:
    with pytest.raises(ValueError):
        RuleConfig(**kwargs)

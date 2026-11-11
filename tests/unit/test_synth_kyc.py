from decimal import Decimal

from app.synth.kyc import ELIGIBLE_TYPES, RISK_LEVELS, build_profile
from app.synth.rng import entity_seed

IDS = [f"ENT{i}" for i in range(200)]


def _all(seed: int, ids: list[str]) -> list:
    return [
        build_profile(seed, e, ELIGIBLE_TYPES[i % 4], Decimal(1000 + i))
        for i, e in enumerate(ids)
    ]


def test_entity_seed_is_stable_and_seed_dependent() -> None:
    assert entity_seed(42, "ENT1") == entity_seed(42, "ENT1")
    assert entity_seed(42, "ENT1") != entity_seed(43, "ENT1")
    assert entity_seed(42, "ENT1") != entity_seed(42, "ENT2")
    assert entity_seed(42, "ENT1", "media") != entity_seed(42, "ENT1")


def test_same_seed_identical_profiles() -> None:
    assert _all(42, IDS) == _all(42, IDS)


def test_profiles_do_not_depend_on_order() -> None:
    fwd = {p.entity_id: p for p in _all(42, IDS)}
    rev = {
        p.entity_id: p
        for p in [
            build_profile(42, e, ELIGIBLE_TYPES[i % 4], Decimal(1000 + i))
            for i, e in reversed(list(enumerate(IDS)))
        ]
    }
    assert fwd == rev


def test_different_seed_differs() -> None:
    a, b = _all(1, IDS), _all(2, IDS)
    assert sum(x.full_name != y.full_name for x, y in zip(a, b, strict=True)) > 150


def test_profile_fields_are_valid() -> None:
    for p in _all(42, IDS):
        assert p.full_name and "#" not in p.full_name
        assert p.occupation and p.country and len(p.country) == 2
        assert p.risk_rating in RISK_LEVELS
        assert p.expected_monthly_volume > 0


def test_volume_scales_real_activity_by_bounded_multiplier() -> None:
    for i in range(50):
        p = build_profile(42, f"E{i}", "Individual", Decimal(10_000))
        assert Decimal(5_000) <= p.expected_monthly_volume <= Decimal(20_000)


def test_volume_falls_back_when_no_activity() -> None:
    for avg in (None, Decimal(0)):
        p = build_profile(42, "E1", "Corporation", avg)
        assert p.expected_monthly_volume > 0


def test_risk_mix_is_not_degenerate() -> None:
    ratings = {p.risk_rating for p in _all(42, [f"E{i}" for i in range(2000)])}
    assert ratings == set(RISK_LEVELS)

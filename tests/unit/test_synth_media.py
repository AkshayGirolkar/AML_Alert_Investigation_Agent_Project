import json
import re
from collections import Counter

import pytest

from app.synth.media import (
    SUPPORTED_PLACEHOLDERS,
    MediaCandidate,
    MediaPlan,
    build_article,
    load_templates,
    select_media_entities,
)

T = load_templates()
PH = re.compile(r"\{(\w+)\}")


def _strings(theme: dict) -> list[str]:
    return [
        s for k in ("headlines", "openings", "details", "closings") for s in theme[k]
    ]


def test_templates_are_valid_json_with_all_parts() -> None:
    assert T["version"] == 1 and len(T["themes"]) == 23
    for th in T["themes"]:
        assert th["category"] in ("laundering", "decoy")
        assert set(th["applies_to"]) <= {"person", "org"} and th["applies_to"]
        assert th["headlines"] and th["openings"] and len(th["details"]) >= 2
        assert th["closings"]


def test_only_supported_placeholders_are_used() -> None:
    used = {p for th in T["themes"] for s in _strings(th) for p in PH.findall(s)}
    assert used <= SUPPORTED_PLACEHOLDERS


def test_every_category_covers_person_and_org() -> None:
    for cat in ("laundering", "decoy"):
        for who in ("person", "org"):
            assert any(
                t["category"] == cat and who in t["applies_to"] for t in T["themes"]
            )


def _cands(n: int, launderers: int) -> list[MediaCandidate]:
    return [
        MediaCandidate(
            f"E{i}", "Individual" if i % 3 else "Corporation", i < launderers
        )
        for i in range(n)
    ]


def test_selection_is_deterministic_order_independent_and_two_percent() -> None:
    c = _cands(5000, 800)
    a = select_media_entities(42, c)
    b = select_media_entities(42, list(reversed(c)))
    assert a == b
    assert abs(len(a) / 5000 - 0.02) <= 0.005
    assert select_media_entities(43, c) != a


def test_selection_mix_has_launderers_and_decoys_and_only_decoys_are_clean() -> None:
    c = _cands(5000, 800)
    plans = select_media_entities(42, c)
    flagged = {x.entity_id: x.is_launderer for x in c}
    assert any(not p.is_decoy for p in plans) and any(p.is_decoy for p in plans)
    for p in plans:
        if p.is_decoy:
            assert not flagged[p.entity_id]
        else:
            assert flagged[p.entity_id]
    # some launderers get no media
    assert sum(not p.is_decoy for p in plans) < 800


def test_selection_without_launderers_is_all_decoys() -> None:
    plans = select_media_entities(42, _cands(1000, 0))
    assert plans and all(p.is_decoy for p in plans)


def _article(i: int, kind: str = "Individual", decoy: bool = False, seed: int = 42):
    return build_article(
        T, MediaPlan(f"E{i}", kind, decoy), f"Test Name{i} Person", seed
    )


def test_article_has_no_unfilled_placeholder() -> None:
    for i in range(300):
        for kind in ("Individual", "Corporation"):
            for decoy in (False, True):
                a = _article(i, kind, decoy)
                assert not re.search(r"\{[^}]*\}", a.headline + a.body), a
                assert f"Test Name{i} Person" in a.headline + a.body


def test_article_is_deterministic_and_seed_dependent() -> None:
    assert _article(1) == _article(1)
    assert _article(1).body != _article(1, seed=7).body


def test_article_theme_matches_category_and_entity_kind() -> None:
    themes = {t["id"]: t for t in T["themes"]}
    for i in range(200):
        a = _article(i, "Individual", False)
        assert themes[a.theme]["category"] == "laundering"
        assert "person" in themes[a.theme]["applies_to"]
        b = _article(i, "Partnership", True)
        assert themes[b.theme]["category"] == "decoy"
        assert "org" in themes[b.theme]["applies_to"]


def test_template_combinations_are_diverse() -> None:
    combos = Counter(
        _article(i, "Individual" if i % 2 else "Corporation", i % 5 == 0).combo
        for i in range(3300)
    )
    assert max(combos.values()) / 3300 <= 0.05
    assert len(combos) > 1000


def test_bodies_differ_between_entities() -> None:
    bodies = {_article(i).body for i in range(200)}
    assert len(bodies) > 190


def test_unknown_placeholder_fails_loudly() -> None:
    bad = json.loads(json.dumps(T))
    for th in bad["themes"]:
        th["headlines"] = ["{nope} appears"]
    with pytest.raises(KeyError):
        build_article(bad, MediaPlan("E1", "Individual", False), "A B", 1)

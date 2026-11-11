import random

import pytest

from app.synth.planter import choose_plants, reorder_name, typo_name


def _edit_distance(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def test_reorder_keeps_words_but_changes_order() -> None:
    out = reorder_name("Viktor Aleksandrovich Petrenko", random.Random(1))
    assert out != "Viktor Aleksandrovich Petrenko"
    assert sorted(out.split()) == sorted(["Viktor", "Aleksandrovich", "Petrenko"])


def test_reorder_two_words_swaps() -> None:
    assert reorder_name("Chen Wei", random.Random(1)) == "Wei Chen"


def test_reorder_rejects_single_word_or_identical_words() -> None:
    with pytest.raises(ValueError):
        reorder_name("Madonna", random.Random(1))
    with pytest.raises(ValueError):
        reorder_name("Bo Bo", random.Random(1))


def test_typo_is_one_substitution() -> None:
    for s in range(30):
        out = typo_name("Northern Lights Trading", random.Random(s))
        assert out != "Northern Lights Trading"
        assert len(out) == len("Northern Lights Trading")
        assert _edit_distance(out, "Northern Lights Trading") == 1
        assert out[0] == "N"  # first letter is left alone


def test_typo_rejects_names_without_letters() -> None:
    with pytest.raises(ValueError):
        typo_name("123 456", random.Random(1))


ENTS = [(f"E{i}", "Individual" if i % 2 else "Corporation") for i in range(60)] + [
    ("C1", "Country"),
    ("S1", "Sole Proprietorship"),
]
SANC = (
    [
        (f"P{i}", f"Firstname{chr(97 + i % 20)} Lastname{chr(97 + i % 7)}x", "Person")
        for i in range(40)
    ]
    + [
        (f"C{i}", f"Acme{chr(97 + i % 20)} Holdings Group{i}", "Company")
        for i in range(40)
    ]
    + [("V1", "Sea Falcon", "Vessel")]
)


def test_choose_plants_is_deterministic_and_order_independent() -> None:
    a = choose_plants(42, ENTS, SANC, 15)
    b = choose_plants(42, list(reversed(ENTS)), list(reversed(SANC)), 15)
    assert a == b and len(a) == 15
    assert choose_plants(43, ENTS, SANC, 15) != a


def test_choose_plants_pairs_types_and_variety() -> None:
    plants = choose_plants(42, ENTS, SANC, 15)
    types = dict(ENTS)
    stype = {s[0]: s[2] for s in SANC}
    assert {p.variant_kind for p in plants} == {"exact", "reordered", "typo"}
    assert len({p.entity_id for p in plants}) == 15
    assert len({p.source_id for p in plants}) == 15
    for p in plants:
        assert types[p.entity_id] in ("Individual", "Corporation")
        want = {
            "Individual": {"Person"},
            "Corporation": {"Company", "Organization", "LegalEntity"},
        }
        assert stype[p.source_id] in want[types[p.entity_id]]
        assert p.planted_name


def test_choose_plants_caps_at_available_candidates() -> None:
    plants = choose_plants(42, [("E1", "Individual")], SANC, 15)
    assert len(plants) == 1

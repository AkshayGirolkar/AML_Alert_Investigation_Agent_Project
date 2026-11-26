from pathlib import Path

from app.eval.dataset import UNTYPED, Candidate, EvalDataset, select_items

TYPES = ["CYCLE", "FAN-IN", "STACK"]
RULES = ["fan_out", "structuring"]


def make_candidates() -> list[Candidate]:
    out: list[Candidate] = []
    i = 0
    for t in TYPES:
        for _ in range(20):
            i += 1
            out.append(Candidate(i, "fan_in", True, t))
    for _ in range(4):
        i += 1
        out.append(Candidate(i, "fan_in", True, None))  # untyped true alerts
    for r in RULES:
        for _ in range(200):
            i += 1
            out.append(Candidate(i, r, False, None))
    return out


def test_selection_is_deterministic_and_stratified() -> None:
    cands = make_candidates()
    a, strata_a = select_items(cands, 40, seed=7)
    b, strata_b = select_items(list(reversed(cands)), 40, seed=7)
    assert a == b and strata_a == strata_b  # input order does not matter
    assert len(a) == 40 and len({c.alert_id for c in a}) == 40
    assert sum(c.truth for c in a) == 20
    # even spread: 20 true over 4 groups; UNTYPED only has 4, the rest fill alphabetically
    true_strata = {k: v for k, v in strata_a.items() if k.startswith("true:")}
    assert true_strata == {
        "true:CYCLE": 6,
        "true:FAN-IN": 5,
        "true:STACK": 5,
        f"true:{UNTYPED}": 4,
    }
    assert strata_a["false:fan_out"] == strata_a["false:structuring"] == 10


def test_different_seed_changes_the_pick() -> None:
    cands = make_candidates()
    assert select_items(cands, 40, seed=1)[0] != select_items(cands, 40, seed=2)[0]


def test_small_arm_is_topped_up_by_the_other() -> None:
    cands = [Candidate(i, "fan_in", True, "CYCLE") for i in range(1, 4)] + [
        Candidate(i, "fan_out", False, None) for i in range(10, 60)
    ]
    items, strata = select_items(cands, 20, seed=1)
    assert len(items) == 20
    assert strata["true:CYCLE"] == 3 and strata["false:fan_out"] == 17


def test_fewer_alerts_than_size() -> None:
    cands = [Candidate(1, "fan_out", False, None)]
    items, _ = select_items(cands, 10, seed=1)
    assert len(items) == 1


def test_roundtrip_is_byte_identical(tmp_path: Path) -> None:
    items, strata = select_items(make_candidates(), 20, seed=3)
    ds = EvalDataset("t", 3, 20, strata, items)
    p = tmp_path / "d.json"
    ds.save(p)
    first = p.read_text()
    loaded = EvalDataset.load(p)
    assert loaded == ds
    loaded.save(p)
    assert p.read_text() == first

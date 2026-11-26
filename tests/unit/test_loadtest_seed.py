"""F16 unit tests: load-set selection and safety checks. No DB."""

import json
from pathlib import Path

import pytest

from app.loadtest.seed import (
    LoadSet,
    eval_alert_ids,
    is_local,
    order_key,
    select_alert_ids,
)

POOL = list(range(1, 301))


def test_selection_is_deterministic_and_order_independent() -> None:
    a = select_alert_ids(POOL, set(), 50, seed=1)
    b = select_alert_ids(list(reversed(POOL)), set(), 50, seed=1)
    assert a == b and len(a) == 50 and len(set(a)) == 50


def test_seed_changes_selection() -> None:
    assert select_alert_ids(POOL, set(), 50, 1) != select_alert_ids(POOL, set(), 50, 2)


def test_excluded_ids_are_never_selected() -> None:
    excluded = set(range(1, 101))
    ids = select_alert_ids(POOL, excluded, 200, seed=1)
    assert not set(ids) & excluded and len(ids) == 200


def test_too_few_eligible_alerts_raises() -> None:
    with pytest.raises(ValueError, match="only 5 eligible"):
        select_alert_ids([1, 2, 3, 4, 5, 6], {6}, 10, seed=1)


def test_duplicates_in_pool_are_collapsed() -> None:
    assert sorted(select_alert_ids([1, 1, 2, 2, 3], set(), 3, 1)) == [1, 2, 3]


def test_order_key_is_stable() -> None:
    assert order_key(42, 7) == order_key(42, 7) != order_key(43, 7)


def test_manifest_roundtrip(tmp_path: Path) -> None:
    ls = LoadSet("load-3", 42, [5, 1, 9], excluded_eval=100)
    ls.save(tmp_path / "d" / "m.json")
    assert LoadSet.load(tmp_path / "d" / "m.json") == ls
    assert json.loads(ls.to_json())["count"] == 3


def test_eval_ids_read_and_missing_file(tmp_path: Path) -> None:
    f = tmp_path / "e.json"
    f.write_text(json.dumps({"items": [{"alert_id": 3}, {"alert_id": 8}]}))
    assert eval_alert_ids(f) == {3, 8}
    assert eval_alert_ids(tmp_path / "missing.json") == set()


@pytest.mark.parametrize(
    ("url", "local"),
    [
        ("postgresql://u:p@localhost:5433/aml", True),
        ("postgresql://u:p@127.0.0.1/aml", True),
        ("postgresql://u:p@postgres/aml", True),
        ("postgresql://u:p@db.abc.us-east-1.rds.amazonaws.com/aml", False),
        ("postgresql://u:p@10.0.3.4/aml", False),
    ],
)
def test_is_local(url: str, local: bool) -> None:
    assert is_local(url) is local

"""F15: dataset building against a real Postgres (the `aml_test` database)."""

from datetime import datetime, timedelta
from pathlib import Path

from app.eval.dataset import UNTYPED, build_dataset
from tests.integration.test_rules import World, _conn, _db, w  # noqa: F401

START = datetime(2022, 9, 5)


def _seed_alerts(w: World) -> None:  # noqa: F811
    acct = w.account()
    rows: list[tuple[str, bool, str | None]] = []
    for typ, n in (("CYCLE", 6), ("FAN-IN", 6), (None, 5)):
        rows += [("fan_in", True, typ)] * n
    rows += [("fan_out", False, None)] * 15 + [("structuring", False, None)] * 15
    with _conn(w.url) as c:
        for i, (rule, truth, typ) in enumerate(rows):
            c.execute(
                "INSERT INTO alerts (rule_id, account_id, window_start, window_end, "
                "trigger_txn_ids, score, is_true_positive, typology) "
                "VALUES (%s, %s, %s, %s, '{}', 1, %s, %s)",
                (
                    rule,
                    acct,
                    START + timedelta(minutes=i),
                    START + timedelta(hours=1),
                    truth,
                    typ,
                ),
            )


def test_build_is_deterministic_and_matches_labels(w: World, tmp_path: Path) -> None:  # noqa: F811
    _seed_alerts(w)
    a = build_dataset(w.url, 12, seed=5, name="t")
    b = build_dataset(w.url, 12, seed=5, name="t")
    assert a == b
    a.save(tmp_path / "a.json")
    b.save(tmp_path / "b.json")
    assert (tmp_path / "a.json").read_bytes() == (tmp_path / "b.json").read_bytes()

    assert len(a.items) == 12 and sum(a.strata.values()) == 12
    assert sum(i.truth for i in a.items) == 6
    assert set(a.strata) >= {"true:CYCLE", "true:FAN-IN", f"true:{UNTYPED}"}
    # labels in the dataset equal the labels in the table
    with _conn(w.url) as c:
        truth = {
            r[0]: (r[1], r[2])
            for r in c.execute(
                "SELECT alert_id, is_true_positive, typology FROM alerts"
            ).fetchall()
        }
    assert all(truth[i.alert_id] == (i.truth, i.truth_typology) for i in a.items)

    c2 = build_dataset(w.url, 12, seed=6, name="t")
    assert c2.items != a.items

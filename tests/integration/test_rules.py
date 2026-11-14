"""Integration tests for F04 against the Docker Postgres (`aml_test` database).

Each test builds its own tiny dataset (a planted positive plus a benign look-alike per
rule), so results do not depend on the F02 fixtures. Skipped when Postgres is not
reachable.
"""

import os
from collections.abc import Iterator
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
from alembic.config import Config
from sqlalchemy.engine import make_url

from alembic import command
from app.config import get_settings
from app.rules.baseline import compute_baseline
from app.rules.config import ALL_RULE_IDS, RuleConfig
from app.rules.engine import run_rules

ROOT = Path(__file__).parent.parent.parent
TEST_DB = "aml_test"
DAY = datetime(2022, 9, 5)  # all planted behaviour happens on this (UTC) day
CFG = RuleConfig(
    velocity_window_hours=1,
    velocity_min_txns=5,
    fan_window_hours=24,
    fan_min_degree=4,
    round_trip_window_hours=24,
    round_trip_max_hops=3,
    structuring_limit=10_000,
    structuring_margin=0.10,
    structuring_min_txns=3,
    structuring_window_hours=24,
    high_risk_countries=("IR",),
    high_risk_ratings=("high",),
    high_risk_window_hours=24,
)


def _alembic_cfg() -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    return cfg


def _alembic(action: str, rev: str) -> None:
    prev = os.environ.get("POSTGRES_DB")
    os.environ["POSTGRES_DB"] = TEST_DB
    try:
        getattr(command, action)(_alembic_cfg(), rev)
    finally:
        if prev is None:
            del os.environ["POSTGRES_DB"]
        else:
            os.environ["POSTGRES_DB"] = prev


def _conn(url: str) -> psycopg.Connection:
    u = make_url(url).set(drivername="postgresql")
    return psycopg.connect(u.render_as_string(hide_password=False), autocommit=True)


def _q(url: str, sql: str, params: tuple | None = None) -> list[tuple]:
    with _conn(url) as c:
        return c.execute(sql, params).fetchall()


@pytest.fixture(scope="module")
def _db() -> str:
    base = make_url(get_settings().sync_database_url)
    admin = base.set(drivername="postgresql").render_as_string(hide_password=False)
    try:
        with psycopg.connect(admin, autocommit=True, connect_timeout=3) as conn:
            if not conn.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s", (TEST_DB,)
            ).fetchone():
                conn.execute(f"CREATE DATABASE {TEST_DB}")
    except psycopg.OperationalError:
        pytest.skip("Postgres is not reachable")
    _alembic("upgrade", "head")
    return base.set(database=TEST_DB).render_as_string(hide_password=False)


class World:
    """Builds a tiny database: accounts, KYC, transactions, ground truth."""

    def __init__(self, url: str) -> None:
        self.url = url
        self._acct = 0
        self._txn = 0
        with _conn(url) as c:
            c.execute("INSERT INTO banks VALUES (1, 'Test Bank')")

    def account(self, country: str = "US", rating: str = "low") -> int:
        """Create entity + KYC profile + one account; return the account_id."""
        self._acct += 1
        ent = f"E{self._acct}"
        with _conn(self.url) as c:
            c.execute(
                "INSERT INTO entities VALUES (%s, %s, 'Individual')",
                (ent, f"Name {ent}"),
            )
            c.execute(
                "INSERT INTO kyc_profiles VALUES (%s, %s, 'clerk', %s, 1000, %s)",
                (ent, f"Person {self._acct}", country, rating),
            )
            row = c.execute(
                "INSERT INTO accounts (bank_id, account_number, entity_id) "
                "VALUES (1, %s, %s) RETURNING account_id",
                (f"ACC{self._acct:06d}", ent),
            ).fetchone()
        assert row is not None
        return int(row[0])

    def txn(
        self,
        src: int,
        dst: int,
        ts: datetime,
        amount: float = 100.0,
        currency: str = "US Dollar",
        laundering: bool = False,
    ) -> int:
        self._txn += 1
        with _conn(self.url) as c:
            c.execute(
                "INSERT INTO transactions (txn_id, ts, from_account_id, to_account_id, "
                "amount_received, amount_paid, receiving_currency, payment_currency, "
                "payment_format, is_laundering) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'ACH',%s)",
                (
                    self._txn,
                    ts,
                    src,
                    dst,
                    amount,
                    amount,
                    currency,
                    currency,
                    laundering,
                ),
            )
        return self._txn

    def attempt(self, typology: str, txn_ids: list[int]) -> int:
        with _conn(self.url) as c:
            row = c.execute(
                "INSERT INTO laundering_attempts (attempt_id, typology, detail_raw) "
                "VALUES ((SELECT coalesce(max(attempt_id),0)+1 FROM laundering_attempts), "
                "%s, '') RETURNING attempt_id",
                (typology,),
            ).fetchone()
            assert row is not None
            for t in txn_ids:
                c.execute(
                    "INSERT INTO laundering_attempt_transactions VALUES (%s, %s)",
                    (row[0], t),
                )
        return int(row[0])


@pytest.fixture
def w(_db: str) -> Iterator[World]:
    with _conn(_db) as c:
        c.execute(
            "TRUNCATE alerts, laundering_attempt_transactions, laundering_attempts, "
            "planted_matches, kyc_profiles, transactions, accounts, entities, banks "
            "RESTART IDENTITY CASCADE"
        )
    yield World(_db)


def _alerts(w: World, rule: str) -> list[dict[str, Any]]:
    rows = _q(
        w.url,
        "SELECT account_id, trigger_txn_ids, window_start, window_end, score "
        "FROM alerts WHERE rule_id = %s ORDER BY account_id",
        (rule,),
    )
    return [
        {
            "account": r[0],
            "txns": sorted(r[1]),
            "start": r[2],
            "end": r[3],
            "score": r[4],
        }
        for r in rows
    ]


def _accounts_flagged(w: World, rule: str) -> set[int]:
    return {a["account"] for a in _alerts(w, rule)}


# ---------------------------------------------------------------- rule: velocity


def test_high_velocity_fires_on_burst_not_on_spread_out_activity(w: World) -> None:
    burst, spread = w.account(), w.account()
    sink = w.account()
    burst_ids = [
        w.txn(burst, sink, DAY + timedelta(hours=10, minutes=5 * i)) for i in range(6)
    ]
    for i in range(6):  # same count, but one per 3 hours -> never 5 in one window
        w.txn(spread, sink, DAY + timedelta(hours=3 * i))
    run_rules(w.url, CFG, ["high_velocity"])
    assert _accounts_flagged(w, "high_velocity") == {burst}
    assert _alerts(w, "high_velocity")[0]["txns"] == sorted(burst_ids)


def test_high_velocity_below_threshold_is_quiet(w: World) -> None:
    a, b = w.account(), w.account()
    for i in range(4):  # one under the threshold of 5
        w.txn(a, b, DAY + timedelta(hours=10, minutes=i))
    run_rules(w.url, CFG, ["high_velocity"])
    assert _alerts(w, "high_velocity") == []


# ---------------------------------------------------------------- rules: fan-out / fan-in


def test_fan_out_fires_for_many_recipients_not_for_one_repeated_recipient(
    w: World,
) -> None:
    spreader, repeater = w.account(), w.account()
    targets = [w.account() for _ in range(4)]
    ids = [
        w.txn(spreader, t, DAY + timedelta(hours=9, minutes=i))
        for i, t in enumerate(targets)
    ]
    for i in range(6):  # many txns, but a single recipient -> degree 1
        w.txn(repeater, targets[0], DAY + timedelta(hours=9, minutes=i))
    run_rules(w.url, CFG, ["fan_out"])
    assert _accounts_flagged(w, "fan_out") == {spreader}
    assert _alerts(w, "fan_out")[0]["txns"] == sorted(ids)


def test_fan_in_fires_for_many_senders_not_for_few(w: World) -> None:
    collector, quiet = w.account(), w.account()
    senders = [w.account() for _ in range(4)]
    ids = [
        w.txn(s, collector, DAY + timedelta(hours=9, minutes=i))
        for i, s in enumerate(senders)
    ]
    for s in senders[:3]:  # only 3 distinct senders -> below threshold of 4
        w.txn(s, quiet, DAY + timedelta(hours=9))
    run_rules(w.url, CFG, ["fan_in"])
    assert _accounts_flagged(w, "fan_in") == {collector}
    assert _alerts(w, "fan_in")[0]["txns"] == sorted(ids)


def test_fan_out_does_not_fire_when_spread_over_different_days(w: World) -> None:
    spreader = w.account()
    for i in range(4):
        w.txn(spreader, w.account(), DAY + timedelta(days=2 * i, hours=9))
    run_rules(w.url, CFG, ["fan_out"])
    assert _alerts(w, "fan_out") == []


# ---------------------------------------------------------------- rule: round trip


def test_round_trip_flags_every_account_in_a_cycle(w: World) -> None:
    a, b, c = w.account(), w.account(), w.account()
    ids = [
        w.txn(a, b, DAY + timedelta(hours=9)),
        w.txn(b, c, DAY + timedelta(hours=10)),
        w.txn(c, a, DAY + timedelta(hours=11)),
    ]
    run_rules(w.url, CFG, ["round_trip"])
    assert _accounts_flagged(w, "round_trip") == {a, b, c}
    for alert in _alerts(w, "round_trip"):
        assert alert["txns"] == sorted(ids)


def test_round_trip_two_hop_back_and_forth(w: World) -> None:
    a, b = w.account(), w.account()
    w.txn(a, b, DAY + timedelta(hours=9))
    w.txn(b, a, DAY + timedelta(hours=10))
    run_rules(w.url, CFG, ["round_trip"])
    assert _accounts_flagged(w, "round_trip") == {a, b}


def test_round_trip_ignores_chain_that_never_closes_and_slow_returns(w: World) -> None:
    a, b, c, d = (w.account() for _ in range(4))
    w.txn(a, b, DAY + timedelta(hours=9))
    w.txn(b, c, DAY + timedelta(hours=10))
    w.txn(c, d, DAY + timedelta(hours=11))  # chain, no return
    e, f = w.account(), w.account()
    w.txn(e, f, DAY + timedelta(hours=9))
    w.txn(f, e, DAY + timedelta(days=20))  # returns, but far outside the window
    run_rules(w.url, CFG, ["round_trip"])
    assert _alerts(w, "round_trip") == []


# ---------------------------------------------------------------- rule: structuring


def test_structuring_fires_just_under_limit_not_on_other_amounts(w: World) -> None:
    smurf, normal, big, euro = (w.account() for _ in range(4))
    sink = w.account()
    ids = [
        w.txn(smurf, sink, DAY + timedelta(hours=9, minutes=i), 9_500.0)
        for i in range(3)
    ]
    for i in range(3):
        w.txn(normal, sink, DAY + timedelta(hours=9, minutes=i), 5_000.0)  # too low
        w.txn(big, sink, DAY + timedelta(hours=9, minutes=i), 10_000.0)  # at the limit
        w.txn(euro, sink, DAY + timedelta(hours=9, minutes=i), 9_500.0, "Euro")
    run_rules(w.url, CFG, ["structuring"])
    assert _accounts_flagged(w, "structuring") == {smurf}
    assert _alerts(w, "structuring")[0]["txns"] == sorted(ids)


def test_structuring_needs_min_txn_count(w: World) -> None:
    a, b = w.account(), w.account()
    for i in range(2):  # threshold is 3
        w.txn(a, b, DAY + timedelta(hours=9, minutes=i), 9_900.0)
    run_rules(w.url, CFG, ["structuring"])
    assert _alerts(w, "structuring") == []


# ---------------------------------------------------------------- rule: high-risk counterparty


def test_high_risk_counterparty_by_country_and_rating(w: World) -> None:
    hits_country, hits_rating, benign = (w.account() for _ in range(3))
    iran = w.account(country="IR")
    risky = w.account(country="US", rating="high")
    safe = w.account(country="US", rating="low")
    t1 = w.txn(hits_country, iran, DAY + timedelta(hours=9))
    t2 = w.txn(hits_rating, risky, DAY + timedelta(hours=9))
    w.txn(benign, safe, DAY + timedelta(hours=9))
    run_rules(w.url, CFG, ["high_risk_counterparty"])
    flagged = _accounts_flagged(w, "high_risk_counterparty")
    assert {hits_country, hits_rating} <= flagged
    assert benign not in flagged
    by_acct = {a["account"]: a["txns"] for a in _alerts(w, "high_risk_counterparty")}
    assert by_acct[hits_country] == [t1]
    assert by_acct[hits_rating] == [t2]


def test_high_risk_counterparty_covers_incoming_direction(w: World) -> None:
    receiver, iran = w.account(), w.account(country="IR")
    t = w.txn(iran, receiver, DAY + timedelta(hours=9))
    run_rules(w.url, CFG, ["high_risk_counterparty"])
    by_acct = {a["account"]: a["txns"] for a in _alerts(w, "high_risk_counterparty")}
    assert by_acct[receiver] == [t]


def test_high_risk_counterparty_only_for_new_relationships(w: World) -> None:
    old, new = w.account(), w.account()
    iran = w.account(country="IR")
    w.txn(old, iran, DAY - timedelta(days=3))  # relationship already existed
    w.txn(old, iran, DAY + timedelta(hours=9))
    w.txn(new, iran, DAY + timedelta(hours=9))  # first contact
    run_rules(w.url, CFG, ["high_risk_counterparty"])
    # The day-minus-3 txn is itself a first contact (in its own window); the later
    # repeat contact on DAY must not alert.
    in_window = {
        a["account"] for a in _alerts(w, "high_risk_counterparty") if a["start"] == DAY
    }
    assert in_window == {new}


def test_high_risk_counterparty_by_sanctions_name_match(w: World) -> None:
    victim, clean = w.account(), w.account()
    sanctioned, other = w.account(), w.account()
    with _conn(w.url) as c:
        c.execute("TRUNCATE sanctions_entities CASCADE")
        c.execute(
            "INSERT INTO sanctions_entities "
            "(source_id, name, aliases, schema_type, countries, dataset) "
            "VALUES ('S1', 'Viktor Petrenko', '{}', 'Person', '{}', '{}')"
        )
        c.execute(
            "UPDATE kyc_profiles SET full_name = 'Viktor Petrenko' "
            "WHERE entity_id = (SELECT entity_id FROM accounts WHERE account_id = %s)",
            (sanctioned,),
        )
    w.txn(victim, sanctioned, DAY + timedelta(hours=9))
    w.txn(clean, other, DAY + timedelta(hours=9))
    run_rules(w.url, CFG, ["high_risk_counterparty"])
    flagged = _accounts_flagged(w, "high_risk_counterparty")
    assert victim in flagged and clean not in flagged


# ---------------------------------------------------------------- engine behaviour


def test_alert_row_shape(w: World) -> None:
    a, b = w.account(), w.account()
    for i in range(5):
        w.txn(a, b, DAY + timedelta(hours=10, minutes=i))
    run_rules(w.url, CFG, ["high_velocity"])
    (alert,) = _alerts(w, "high_velocity")
    assert alert["start"] <= DAY + timedelta(hours=10) < alert["end"]
    assert alert["end"] - alert["start"] == timedelta(hours=CFG.velocity_window_hours)
    assert 0 < float(alert["score"]) <= 1
    created = _q(w.url, "SELECT created_at FROM alerts")[0][0]
    assert created is not None


def _snapshot(w: World) -> list[tuple]:
    return _q(
        w.url,
        "SELECT rule_id, account_id, window_start, window_end, trigger_txn_ids, "
        "score, is_true_positive, typology FROM alerts ORDER BY rule_id, account_id, "
        "window_start",
    )


def _world_with_every_rule_triggered(w: World) -> None:
    a, b, c = w.account(), w.account(), w.account()
    w.txn(a, b, DAY + timedelta(hours=9))
    w.txn(b, c, DAY + timedelta(hours=10))
    w.txn(c, a, DAY + timedelta(hours=11))
    smurf, sink = w.account(), w.account()
    for i in range(6):
        w.txn(smurf, sink, DAY + timedelta(hours=14, minutes=i), 9_500.0)
    w.txn(w.account(), w.account(country="IR"), DAY + timedelta(hours=15))


def test_run_returns_counts_per_rule_and_is_idempotent(w: World) -> None:
    _world_with_every_rule_triggered(w)
    first = run_rules(w.url, CFG)
    assert set(first) == set(ALL_RULE_IDS)
    assert first["round_trip"] == 3 and first["structuring"] == 1
    snap = _snapshot(w)
    assert len(snap) == sum(first.values())
    second = run_rules(w.url, CFG)
    assert all(n == 0 for n in second.values())  # nothing new inserted
    assert _snapshot(w) == snap


def test_rule_ids_subset_only_runs_those_rules(w: World) -> None:
    _world_with_every_rule_triggered(w)
    out = run_rules(w.url, CFG, ["structuring"])
    assert set(out) == {"structuring"}
    assert {r[0] for r in _snapshot(w)} == {"structuring"}


def test_unknown_rule_id_is_rejected(w: World) -> None:
    with pytest.raises(ValueError, match="nope"):
        run_rules(w.url, CFG, ["nope"])


def test_reset_replaces_stale_alerts_after_threshold_change(w: World) -> None:
    a, b = w.account(), w.account()
    for i in range(5):
        w.txn(a, b, DAY + timedelta(hours=10, minutes=i))
    run_rules(w.url, CFG, ["high_velocity"])
    assert len(_snapshot(w)) == 1
    stricter = replace(CFG, velocity_min_txns=9)
    run_rules(w.url, stricter, ["high_velocity"])
    assert len(_snapshot(w)) == 1  # stale alert stays without reset
    run_rules(w.url, stricter, ["high_velocity"], reset=True)
    assert _snapshot(w) == []


def test_reset_only_clears_the_rules_being_rerun(w: World) -> None:
    _world_with_every_rule_triggered(w)
    run_rules(w.url, CFG)
    before = {repr(r) for r in _snapshot(w) if r[0] != "structuring"}
    run_rules(w.url, CFG, ["structuring"], reset=True)
    after = {repr(r) for r in _snapshot(w) if r[0] != "structuring"}
    assert before == after
    assert len([r for r in _snapshot(w) if r[0] == "structuring"]) == 1


# ---------------------------------------------------------------- labeling


def test_labels_true_positive_with_typology_and_untyped_and_benign(w: World) -> None:
    a, b, c, d = (w.account() for _ in range(4))
    # alert 1 (velocity on a): trigger txns are laundering and belong to a CYCLE attempt
    laund = [
        w.txn(a, b, DAY + timedelta(hours=10, minutes=i), laundering=True)
        for i in range(5)
    ]
    w.attempt("CYCLE", laund)
    # alert 2 (velocity on c): laundering but in no attempt -> true positive, no typology
    untyped = [
        w.txn(c, b, DAY + timedelta(hours=12, minutes=i), laundering=True)
        for i in range(5)
    ]
    # alert 3 (velocity on d): benign
    for i in range(5):
        w.txn(d, b, DAY + timedelta(hours=14, minutes=i))
    run_rules(w.url, CFG, ["high_velocity"])
    got = {
        r[0]: (r[1], r[2])
        for r in _q(w.url, "SELECT account_id, is_true_positive, typology FROM alerts")
    }
    assert got == {a: (True, "CYCLE"), c: (True, None), d: (False, None)}
    assert untyped  # silence unused warning: ids exist in the ground truth only


def test_one_laundering_txn_among_benign_ones_makes_the_alert_true(w: World) -> None:
    a, b = w.account(), w.account()
    ids = [w.txn(a, b, DAY + timedelta(hours=10, minutes=i)) for i in range(4)]
    ids.append(w.txn(a, b, DAY + timedelta(hours=10, minutes=30), laundering=True))
    run_rules(w.url, CFG, ["high_velocity"])
    assert _q(w.url, "SELECT is_true_positive FROM alerts") == [(True,)]
    assert len(ids) == 5


def test_mixed_typologies_pick_the_most_frequent(w: World) -> None:
    a, b = w.account(), w.account()
    t = [
        w.txn(a, b, DAY + timedelta(hours=10, minutes=i), laundering=True)
        for i in range(5)
    ]
    w.attempt("FAN-OUT", t[:2])
    w.attempt("STACK", t[2:])
    run_rules(w.url, CFG, ["high_velocity"])
    assert _q(w.url, "SELECT typology FROM alerts") == [("STACK",)]


# ---------------------------------------------------------------- baseline report


def test_baseline_precision_recall_per_rule_and_typology(w: World) -> None:
    # Ground truth: attempt 1 (CYCLE) is caught by a velocity alert; attempt 2 (STACK)
    # is missed by every rule; one extra laundering txn belongs to no attempt (untyped)
    # and is also missed. One benign velocity alert is a false positive.
    a, b, c, d, e = (w.account() for _ in range(5))
    caught = [
        w.txn(a, b, DAY + timedelta(hours=10, minutes=i), laundering=True)
        for i in range(5)
    ]
    w.attempt("CYCLE", caught)
    missed = w.txn(c, b, DAY + timedelta(hours=12), laundering=True)
    w.attempt("STACK", [missed])
    w.txn(e, b, DAY + timedelta(hours=13), laundering=True)  # untyped, missed
    for i in range(5):  # benign burst -> false positive alert
        w.txn(d, b, DAY + timedelta(hours=14, minutes=i))
    run_rules(w.url, CFG, ["high_velocity"])

    rep = compute_baseline(w.url)
    assert rep.overall.alerts == 2
    assert rep.overall.true_positive_alerts == 1
    assert rep.overall.precision == pytest.approx(0.5)
    assert rep.overall.fp_rate == pytest.approx(0.5)
    assert rep.overall.recall == pytest.approx(
        5 / 7
    )  # txn level: 5 of 7 laundering txns
    assert rep.attempt_recall == pytest.approx(0.5)  # attempt level: 1 of 2 attempts
    assert rep.by_rule["high_velocity"].precision == pytest.approx(0.5)
    assert rep.by_typology["CYCLE"].recall == pytest.approx(1.0)
    assert rep.by_typology["STACK"].recall == pytest.approx(0.0)
    assert rep.by_typology["untyped"].recall == pytest.approx(0.0)


def test_baseline_with_no_alerts_does_not_divide_by_zero(w: World) -> None:
    a, b = w.account(), w.account()
    w.txn(a, b, DAY + timedelta(hours=10), laundering=True)
    rep = compute_baseline(w.url)
    assert rep.overall.alerts == 0
    assert rep.overall.precision is None
    assert rep.overall.recall == 0.0


# ---------------------------------------------------------------- migration (keep last)


def test_migration_0003_up_down_up(_db: str) -> None:
    q = "SELECT count(*) FROM information_schema.tables WHERE table_name = 'alerts'"
    assert _q(_db, q) == [(1,)]
    uniq = _q(
        _db,
        "SELECT count(*) FROM pg_indexes WHERE tablename = 'alerts' "
        "AND indexdef ILIKE '%UNIQUE%'",
    )
    assert uniq[0][0] >= 1
    _alembic("downgrade", "0002")
    assert _q(_db, q) == [(0,)]
    assert _q(_db, "SELECT count(*) FROM pg_tables WHERE tablename='kyc_profiles'") == [
        (1,)
    ]
    _alembic("upgrade", "head")
    assert _q(_db, q) == [(1,)]

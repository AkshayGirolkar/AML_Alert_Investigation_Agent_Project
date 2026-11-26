"""F16: build_load_set and reset against the `aml_test` database."""

from datetime import datetime, timedelta

import pytest

from app.loadtest.seed import build_load_set, reset
from tests.integration.test_rules import World, _conn, _db, _q, w  # noqa: F401

START = datetime(2022, 9, 1)


def _alerts(w: World, n: int) -> list[int]:  # noqa: F811
    acct = w.account()
    ids = []
    with _conn(w.url) as c:
        for i in range(n):
            row = c.execute(
                "INSERT INTO alerts (rule_id, account_id, window_start, window_end, "
                "trigger_txn_ids, score) VALUES ('fan_in', %s, %s, %s, '{}', 1) "
                "RETURNING alert_id",
                (acct, START + timedelta(minutes=i), START + timedelta(hours=1)),
            ).fetchone()
            assert row is not None
            ids.append(int(row[0]))
    return ids


def _case(url: str, alert_id: int) -> int:
    with _conn(url) as c:
        row = c.execute(
            "INSERT INTO cases (alert_id, status) VALUES (%s, 'completed') "
            "RETURNING case_id",
            (alert_id,),
        ).fetchone()
        assert row is not None
        case_id = int(row[0])
        c.execute(
            "INSERT INTO llm_calls (case_id, call_seq, node, provider, model, "
            "input_tokens, output_tokens, cost_usd, latency_ms, attempt, fallback) "
            "VALUES (%s, 1, 'plan', 'synthetic', 'synthetic-llm', 1, 1, 0, 1, 1, false)",
            (case_id,),
        )
        c.execute(
            "INSERT INTO audit_log (case_id, actor, action) VALUES (%s, 'x', 'y')",
            (case_id,),
        )
    return case_id


def test_pool_skips_alerts_with_cases_and_eval_alerts(w: World) -> None:  # noqa: F811
    ids = _alerts(w, 10)
    _case(w.url, ids[0])
    ls = build_load_set(w.url, "t", 6, seed=3, eval_ids={ids[1], ids[2]})
    assert len(ls.alert_ids) == 6
    assert not set(ls.alert_ids) & {ids[0], ids[1], ids[2]}
    assert ls == build_load_set(w.url, "t", 6, seed=3, eval_ids={ids[1], ids[2]})


def test_reset_deletes_only_listed_alerts(w: World) -> None:  # noqa: F811
    a, b, c = _alerts(w, 3)
    for x in (a, b, c):
        _case(w.url, x)
    counts = reset(w.url, [a, b], eval_ids=set())
    assert counts["cases"] == 2 and counts["llm_calls"] == 2
    assert counts["audit_log"] == 2
    left = _q(w.url, "SELECT alert_id FROM cases")
    assert left == [(c,)]
    assert _q(w.url, "SELECT count(*) FROM llm_calls") == [(1,)]


def test_reset_refuses_eval_alerts(w: World) -> None:  # noqa: F811
    (a,) = _alerts(w, 1)
    _case(w.url, a)
    with pytest.raises(ValueError, match="eval-set"):
        reset(w.url, [a], eval_ids={a})
    assert _q(w.url, "SELECT count(*) FROM cases") == [(1,)]

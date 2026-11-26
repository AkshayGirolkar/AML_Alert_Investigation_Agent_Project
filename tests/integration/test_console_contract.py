# ruff: noqa: F811
"""F10.5 integration: grants, views, notify trigger and the relay on the Docker Postgres.

Skipped when Postgres is not reachable. The role is exercised with `SET ROLE` so the
tests need no password; the grants are what is under test.
"""

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import psycopg
import pytest
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.db.cases import SqlCaseStore
from app.worker.console_relay import ConsoleRelay
from tests.integration.test_rules import World, _conn
from tests.integration.test_worker_resume import _db, w  # noqa: F401
from tests.unit.test_api import FakeQueue


@pytest.fixture(autouse=True)
def _clean_requests(w: World) -> None:
    with _conn(w.url) as c:  # no FK to the tables `w` truncates, so clear it here
        c.execute("TRUNCATE console.requests")


@pytest.fixture
async def engine(w: World) -> AsyncIterator[AsyncEngine]:
    url = make_url(w.url).set(drivername="postgresql+asyncpg")
    eng = create_async_engine(url.render_as_string(hide_password=False))
    yield eng
    await eng.dispose()


def _alert(w: World) -> int:
    acct = w.account()
    with _conn(w.url) as c:
        row = c.execute(
            "INSERT INTO alerts (rule_id, account_id, window_start, window_end, "
            "trigger_txn_ids, score) VALUES ('velocity', %s, now(), now(), '{}', 0.9) "
            "RETURNING alert_id",
            (acct,),
        ).fetchone()
    assert row
    return int(row[0])


def _request(w: World, kind: str, **cols: Any) -> str:
    rid = str(uuid.uuid4())
    payload = json.dumps(cols.pop("payload", {}))
    with _conn(w.url) as c:
        c.execute("SET ROLE console_app")
        c.execute(
            "INSERT INTO console.requests (request_id, kind, alert_id, case_id, "
            "payload, requested_by) VALUES (%s, %s, %s, %s, %s::jsonb, 'analyst-7')",
            (rid, kind, cols.get("alert_id"), cols.get("case_id"), payload),
        )
    return rid


def _status(w: World, rid: str) -> tuple[str, dict[str, Any]]:
    with _conn(w.url) as c:
        row = c.execute(
            "SELECT status, result FROM console.requests WHERE request_id = %s", (rid,)
        ).fetchone()
    assert row
    return row[0], row[1]


def _paused_case(w: World, alert_id: int) -> int:
    with _conn(w.url) as c:
        row = c.execute(
            "INSERT INTO cases (alert_id, status, verdict, evidence) VALUES "
            '(%s, \'awaiting_review\', \'{"disposition":"escalate","risk_score":0.9}\', '
            '\'[{"id":"E1"}]\') RETURNING case_id',
            (alert_id,),
        ).fetchone()
    assert row
    return int(row[0])


def _relay(engine: AsyncEngine, queue: FakeQueue) -> ConsoleRelay:
    return ConsoleRelay(engine, SqlCaseStore(engine), queue)  # type: ignore[arg-type]


# ---- grants -------------------------------------------------------------------------


def test_console_role_can_read_views_but_not_base_tables(w: World) -> None:
    with _conn(w.url) as c:
        c.execute("SET ROLE console_app")
        c.execute("SELECT * FROM console.v_alert_queue").fetchall()
        c.execute("SELECT * FROM console.v_case_detail").fetchall()
        c.execute("SELECT * FROM console.v_requests").fetchall()
    for table in ("alerts", "cases", "decisions", "audit_log"):
        with _conn(w.url) as c:
            c.execute("SET ROLE console_app")
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                c.execute(f"SELECT * FROM public.{table}")


def test_console_role_cannot_update_delete_or_set_status(w: World) -> None:
    rid = _request(w, "investigate", alert_id=1)
    for sql in (
        "UPDATE console.requests SET status = 'applied'",
        "DELETE FROM console.requests",
        "SELECT * FROM console.requests",
    ):
        with _conn(w.url) as c:
            c.execute("SET ROLE console_app")
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                c.execute(sql)
    with _conn(w.url) as c:
        c.execute("SET ROLE console_app")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute(
                "INSERT INTO console.requests (request_id, kind, alert_id, requested_by, "
                "status) VALUES (%s, 'investigate', 1, 'x', 'applied')",
                (str(uuid.uuid4()),),
            )
    assert _status(w, rid)[0] == "pending"


def test_duplicate_request_id_is_rejected(w: World) -> None:
    rid = _request(w, "investigate", alert_id=1)
    with _conn(w.url) as c:
        c.execute("SET ROLE console_app")
        with pytest.raises(psycopg.errors.UniqueViolation):
            c.execute(
                "INSERT INTO console.requests (request_id, kind, alert_id, requested_by)"
                " VALUES (%s, 'investigate', 1, 'x')",
                (rid,),
            )


def test_alert_queue_view_hides_ground_truth(w: World) -> None:
    _alert(w)
    with _conn(w.url) as c:
        cols = {
            d.name
            for d in c.execute("SELECT * FROM console.v_alert_queue").description or []
        }
    assert "is_true_positive" not in cols
    assert (
        "typology" in cols
    )  # the agent's typology, from the verdict, not ground truth


def test_insert_sends_a_notification(w: World) -> None:
    with _conn(w.url) as listener:
        listener.execute("LISTEN console_requests")
        rid = _request(w, "investigate", alert_id=1)
        got = next(iter(listener.notifies(timeout=3, stop_after=1)), None)
    assert got is not None and got.payload == rid


# ---- views --------------------------------------------------------------------------


def test_case_detail_shows_evidence_kyc_and_decision(w: World) -> None:
    case_id = _paused_case(w, _alert(w))
    with _conn(w.url) as c:
        c.execute("SET ROLE console_app")
        row = c.execute(
            "SELECT evidence, full_name, decision, case_status "
            "FROM console.v_case_detail WHERE case_id = %s",
            (case_id,),
        ).fetchone()
    assert row == ([{"id": "E1"}], "Person 1", None, "awaiting_review")


# ---- relay --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_investigate_request_creates_one_case_and_enqueues(
    w: World,
    engine: AsyncEngine,
) -> None:
    alert = _alert(w)
    r1 = _request(w, "investigate", alert_id=alert)
    r2 = _request(w, "investigate", alert_id=alert)
    queue = FakeQueue()
    assert await _relay(engine, queue).drain() == 2
    assert _status(w, r1)[0] == _status(w, r2)[0] == "applied"
    with _conn(w.url) as c:
        assert c.execute("SELECT count(*) FROM cases").fetchone() == (1,)
    assert len(queue.sent) == 2  # re-send while queued is harmless (F07)


@pytest.mark.asyncio
async def test_investigate_unknown_alert_is_rejected_404(
    w: World,
    engine: AsyncEngine,
) -> None:
    rid = _request(w, "investigate", alert_id=999)
    await _relay(engine, FakeQueue()).drain()
    status, result = _status(w, rid)
    assert status == "rejected" and result["code"] == 404


@pytest.mark.asyncio
async def test_decision_on_waiting_case_is_recorded_audited_and_resumed(
    w: World,
    engine: AsyncEngine,
) -> None:
    case_id = _paused_case(w, _alert(w))
    rid = _request(
        w, "decision", case_id=case_id, payload={"decision": "approve", "notes": "ok"}
    )
    queue = FakeQueue()
    await _relay(engine, queue).drain()
    assert _status(w, rid)[0] == "applied"
    assert len(queue.resumes) == 1
    with _conn(w.url) as c:
        dec = c.execute("SELECT decision, analyst_id FROM decisions").fetchall()
        audit = c.execute("SELECT actor, detail FROM audit_log").fetchall()
    assert dec == [("approve", "analyst-7")]
    assert audit[0][0] == "analyst-7" and audit[0][1]["via"] == "console"
    assert audit[0][1]["request_id"] == rid


@pytest.mark.asyncio
async def test_decision_on_non_waiting_case_is_rejected_and_changes_nothing(
    w: World,
    engine: AsyncEngine,
) -> None:
    case_id = _paused_case(w, _alert(w))
    with _conn(w.url) as c:
        c.execute("UPDATE cases SET status = 'running'")
    rid = _request(w, "decision", case_id=case_id, payload={"decision": "approve"})
    queue = FakeQueue()
    await _relay(engine, queue).drain()
    status, result = _status(w, rid)
    assert status == "rejected" and result["code"] == 409
    with _conn(w.url) as c:
        assert c.execute("SELECT count(*) FROM decisions").fetchone() == (0,)
    assert queue.resumes == []


@pytest.mark.asyncio
async def test_malformed_decision_payload_is_rejected_400(
    w: World,
    engine: AsyncEngine,
) -> None:
    case_id = _paused_case(w, _alert(w))
    rid = _request(w, "decision", case_id=case_id, payload={"decision": "maybe"})
    await _relay(engine, FakeQueue()).drain()
    status, result = _status(w, rid)
    assert status == "rejected" and result["code"] == 400


@pytest.mark.asyncio
async def test_replaying_an_applied_request_creates_no_second_decision_or_audit_row(
    w: World,
    engine: AsyncEngine,
) -> None:
    case_id = _paused_case(w, _alert(w))
    rid = _request(w, "decision", case_id=case_id, payload={"decision": "reject"})
    relay = _relay(engine, FakeQueue())
    await relay.drain()
    # Simulate a crash after the DB commit but before the request was marked applied.
    with _conn(w.url) as c:
        c.execute("UPDATE console.requests SET status = 'pending', result = NULL")
    await relay.drain()
    assert _status(w, rid)[0] == "applied"
    with _conn(w.url) as c:
        assert c.execute("SELECT count(*) FROM decisions").fetchone() == (1,)
        assert c.execute("SELECT count(*) FROM audit_log").fetchone() == (1,)


@pytest.mark.asyncio
async def test_failing_request_is_retried_then_given_up_without_blocking_others(
    w: World,
    engine: AsyncEngine,
) -> None:
    class Boom(FakeQueue):
        async def send(self, *a: Any, **k: Any) -> None:
            raise RuntimeError("sqs down")

    bad = _request(w, "investigate", alert_id=_alert(w))
    relay = ConsoleRelay(engine, SqlCaseStore(engine), Boom(), max_attempts=2)  # type: ignore[arg-type]
    await relay.process_one()
    assert _status(w, bad)[0] == "pending"  # first failure: stays pending
    await relay.process_one()
    status, result = _status(w, bad)
    assert status == "rejected" and result["code"] == 500


@pytest.mark.asyncio
async def test_two_relays_never_apply_the_same_request_twice(
    w: World,
    engine: AsyncEngine,
) -> None:
    import asyncio

    for _ in range(6):
        _request(w, "investigate", alert_id=_alert(w))
    queue = FakeQueue()
    counts = await asyncio.gather(
        _relay(engine, queue).drain(), _relay(engine, queue).drain()
    )
    assert sum(counts) == 6 and len(queue.sent) == 6


# ---- v_alert_transactions (0007) ------------------------------------------------------


def test_alert_transactions_view_lists_trigger_txns_without_ground_truth(
    w: World,
) -> None:
    from datetime import datetime

    a, b = w.account(), w.account()
    t1 = w.txn(a, b, datetime(2022, 9, 1, 10, 0), 50.0, laundering=True)
    t2 = w.txn(b, a, datetime(2022, 9, 1, 11, 0), 75.0)
    w.txn(a, b, datetime(2022, 9, 1, 12, 0))  # not a trigger transaction
    with _conn(w.url) as c:
        row = c.execute(
            "INSERT INTO alerts (rule_id, account_id, window_start, window_end, "
            "trigger_txn_ids, score) VALUES ('velocity', %s, now(), now(), %s, 0.9) "
            "RETURNING alert_id",
            (a, [t1, t2]),
        ).fetchone()
        assert row
        alert_id = row[0]
        c.execute("SET ROLE console_app")
        cur = c.execute(
            "SELECT * FROM console.v_alert_transactions WHERE alert_id = %s "
            "ORDER BY ts",
            (alert_id,),
        )
        cols = {d.name for d in cur.description or []}
        rows = cur.fetchall()
    assert [r[1] for r in rows] == [t1, t2]
    assert "is_laundering" not in cols
    assert {"from_account_number", "to_bank", "amount_paid", "payment_format"} <= cols

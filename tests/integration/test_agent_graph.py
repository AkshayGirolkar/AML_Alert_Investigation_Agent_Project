"""F06 integration: the whole graph against the Docker Postgres (`aml_test`).

A scripted LLM stands in for OpenAI; tools, SQL and routing are real. Skipped when
Postgres is not reachable.
"""

import json
import re
from collections.abc import Iterator, Sequence
from datetime import datetime, timedelta
from typing import Any, TypeVar

import psycopg
import pytest
from pydantic import BaseModel
from sqlalchemy.engine import make_url

from app.agent.graph import build_graph
from app.agent.llm import Message
from app.agent.schemas import InvestigationPlan, PlannedCheck, SarDraft, Verdict
from app.agent.tools.registry import make_conn_factory
from app.config import get_settings
from app.synth.embeddings import FakeEmbedder
from tests.integration.test_rules import TEST_DB, World, _alembic, _conn

T = TypeVar("T", bound=BaseModel)
DAY = datetime(2022, 9, 5)
SECRET_TYPOLOGY = "SECRET-GROUND-TRUTH-TYPOLOGY"


class ScriptedLLM:
    """Plans one transactions check; verdicts are scripted but cite real IDs."""

    def __init__(self, verdicts: list[dict[str, Any]]) -> None:
        self.verdicts = verdicts
        self.prompts: list[tuple[str, str]] = []

    def structured(self, schema: type[T], messages: Sequence[Message]) -> T:
        user = messages[-1][1]
        self.prompts.append((schema.__name__, user))
        if schema is InvestigationPlan:
            return schema.model_validate(  # type: ignore[return-value]
                InvestigationPlan(checks=[PlannedCheck(tool="query_transactions")])
            )
        if schema is Verdict:
            v = dict(self.verdicts.pop(0))
            if v.get("evidence_ids") == "REAL":
                v["evidence_ids"] = re.findall(r'"evidence_id": "([^"]+)"', user)[:1]
            return schema.model_validate(v)
        ids = re.findall(r'"evidence_id": "([^"]+)"', user)[:1]
        return schema.model_validate(
            SarDraft(narrative="Suspicious.", evidence_ids=ids)
        )


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


@pytest.fixture
def w(_db: str) -> Iterator[World]:
    with _conn(_db) as c:
        c.execute(
            "TRUNCATE alerts, adverse_media, planted_matches, sanctions_entities, "
            "laundering_attempt_transactions, laundering_attempts, kyc_profiles, "
            "transactions, accounts, entities, banks RESTART IDENTITY CASCADE"
        )
    yield World(_db)


def _alert(w: World) -> int:
    """One account with a few txns and an alert carrying ground-truth labels."""
    a, b = w.account(), w.account()
    txns = [w.txn(a, b, DAY + timedelta(hours=i), 100.0) for i in range(3)]
    with _conn(w.url) as c:
        row = c.execute(
            "INSERT INTO alerts (rule_id, account_id, window_start, window_end, "
            "trigger_txn_ids, score, is_true_positive, typology) "
            "VALUES ('high_velocity', %s, %s, %s, %s, 0.5, true, %s) RETURNING alert_id",
            (a, DAY, DAY + timedelta(days=1), txns, SECRET_TYPOLOGY),
        ).fetchone()
    assert row is not None
    return int(row[0])


def _run(w: World, alert_id: int, llm: ScriptedLLM) -> dict[str, Any]:
    graph = build_graph(
        make_conn_factory(w.url),
        plan_llm=llm,
        embedder=FakeEmbedder(),
        settings=get_settings(),
    )
    return graph.invoke({"alert_id": alert_id})  # type: ignore[no-any-return]


ESCALATE = {
    "disposition": "escalate",
    "risk_score": 0.9,
    "confidence": 0.9,
    "evidence_ids": "REAL",
    "typology": "FAN-OUT",
}
CLOSE = {
    "disposition": "close",
    "risk_score": 0.1,
    "confidence": 0.95,
    "evidence_ids": "REAL",
}
UNKNOWN = {**CLOSE, "evidence_ids": ["query_transactions:doesnotexist"]}


def test_escalation_end_to_end(w: World) -> None:
    llm = ScriptedLLM([ESCALATE])
    state = _run(w, _alert(w), llm)
    assert state["outcome"] == "escalated" and state["grounded"] is True
    assert state["sar_draft"]["narrative"] == "Suspicious."
    ids = {e["evidence_id"] for e in state["evidence"]}
    assert set(state["verdict"]["evidence_ids"]) <= ids and ids
    assert state["tool_calls"] == 1


def test_benign_alert_is_closed(w: World) -> None:
    state = _run(w, _alert(w), ScriptedLLM([CLOSE]))
    assert state["outcome"] == "closed" and state["sar_draft"] is None


def test_ground_truth_never_reaches_state_or_prompts(w: World) -> None:
    llm = ScriptedLLM([ESCALATE])
    state = _run(w, _alert(w), llm)
    blob = json.dumps(state, default=str) + " ".join(p for _, p in llm.prompts)
    assert SECRET_TYPOLOGY not in blob
    assert "is_true_positive" not in blob


def test_unknown_id_is_retried_once_with_feedback(w: World) -> None:
    llm = ScriptedLLM([UNKNOWN, CLOSE])
    state = _run(w, _alert(w), llm)
    assert state["outcome"] == "closed" and state["grounded"] is True
    assert state["grounding_retries"] == 1
    assessed = [p for name, p in llm.prompts if name == "Verdict"]
    assert len(assessed) == 2
    assert "REJECTED" not in assessed[0]
    assert "doesnotexist" in assessed[1]  # feedback names the bad id


def test_still_ungrounded_after_retry_is_forced_to_escalate(w: World) -> None:
    llm = ScriptedLLM([UNKNOWN, UNKNOWN])
    state = _run(w, _alert(w), llm)
    assert state["outcome"] == "escalated" and state["grounded"] is False
    assert "could not be grounded" in state["sar_draft"]["narrative"]
    assert len([n for n, _ in llm.prompts if n == "Verdict"]) == 2  # exactly one retry


def test_unknown_alert_raises(w: World) -> None:
    with pytest.raises(LookupError):
        _run(w, 999_999, ScriptedLLM([]))

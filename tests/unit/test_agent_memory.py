"""F09 unit tests: customer memory, history in prompts, routing rule. No DB, no network."""

from contextlib import nullcontext
from typing import Any

import pytest
from langgraph.store.memory import InMemoryStore

from app.agent import prompts
from app.agent.llm import FakeLLM
from app.agent.memory import (
    CustomerMemory,
    build_record,
    customer_key,
    has_prior_approved_sar,
)
from app.agent.nodes import Nodes
from app.agent.routing import route_verdict
from app.agent.schemas import Verdict
from app.agent.state import InvestigationState
from app.config import Settings


def _state(
    alert_id: int = 1, entity: str | None = "E1", **kw: Any
) -> InvestigationState:
    base: dict[str, Any] = {
        "alert_id": alert_id,
        "alert": {"alert_id": alert_id, "rule_id": "high_velocity", "account_id": "A1"},
        "kyc": {"entity_id": entity} if entity else None,
        "verdict": {
            "disposition": "escalate",
            "risk_score": 0.9,
            "confidence": 0.8,
            "typology": "FAN-OUT",
            "evidence_ids": ["t:1"],
        },
        "outcome": "escalated",
        "decision": "approve",
        "analyst_id": "ana",
        "analyst_notes": "looks real",
    }
    return {**base, **kw}  # type: ignore[typeddict-item,return-value]


def _nodes(store: Any = None, **settings: Any) -> Nodes:
    return Nodes(
        lambda: nullcontext(),  # type: ignore[arg-type,return-value]
        [],
        FakeLLM(),
        FakeLLM(),
        Settings(postgres_password="x", **settings),
        store,
    )


def test_customer_key_uses_entity_else_account() -> None:
    assert customer_key(_state(entity="E7")) == "E7"
    assert customer_key(_state(entity=None)) == "account:A1"


def test_record_has_no_ground_truth() -> None:
    state = _state()
    state["alert"]["is_true_positive"] = True  # even if it leaked into state...
    state["alert"]["typology"] = "SECRET"
    rec = build_record(state)
    assert "SECRET" not in str(rec) and "is_true_positive" not in rec
    assert rec["decision"] == "approve" and rec["analyst_notes"] == "looks real"


async def test_recall_is_newest_first_limited_and_skips_own_alert() -> None:
    mem = CustomerMemory(InMemoryStore())
    for i in (1, 2, 3, 4):
        await mem.record(_state(alert_id=i))
    got = await mem.recall(_state(alert_id=4), limit=2)
    ids = [r["alert_id"] for r in got]
    assert ids == [3, 2]  # newest first, own alert (4) excluded, limit 2


async def test_record_is_idempotent_per_alert() -> None:
    store = InMemoryStore()
    mem = CustomerMemory(store)
    await mem.record(_state(alert_id=5))
    await mem.record(_state(alert_id=5, analyst_notes="updated"))
    items = await store.asearch(("customer_history", "E1"))
    assert len(items) == 1 and items[0].value["analyst_notes"] == "updated"


async def test_customers_are_isolated() -> None:
    mem = CustomerMemory(InMemoryStore())
    await mem.record(_state(alert_id=1, entity="E1"))
    assert await mem.recall(_state(alert_id=2, entity="E2"), limit=5) == []


class BrokenStore(InMemoryStore):
    async def asearch(self, *a: Any, **k: Any) -> Any:
        raise ConnectionError("store down")

    async def aput(self, *a: Any, **k: Any) -> Any:
        raise ConnectionError("store down")


async def test_recall_degrades_but_write_propagates() -> None:
    mem = CustomerMemory(BrokenStore())
    assert await mem.recall(_state(), limit=5) == []
    with pytest.raises(ConnectionError):
        await mem.record(_state())


async def test_nodes_without_store_are_noops() -> None:
    nodes = _nodes(None)
    assert await nodes.recall_history(_state()) == {"history": []}
    assert await nodes.write_memory(_state()) == {}
    assert nodes.prior_sar(_state()) is False


async def test_nodes_roundtrip_through_store() -> None:
    nodes = _nodes(InMemoryStore())
    await nodes.write_memory(_state(alert_id=1))
    out = await nodes.recall_history(_state(alert_id=2))
    assert [r["alert_id"] for r in out["history"]] == [1]


def test_prior_sar_detection_and_setting() -> None:
    approved: list[dict[str, Any]] = [{"decision": "approve"}]
    rejected: list[dict[str, Any]] = [{"decision": "reject"}, {"decision": None}]
    assert has_prior_approved_sar(approved) and not has_prior_approved_sar(rejected)
    assert _nodes().prior_sar(_state(history=approved)) is True
    assert _nodes().prior_sar(_state(history=rejected)) is False
    off = _nodes(memory_block_auto_close_on_prior_sar=False)
    assert off.prior_sar(_state(history=approved)) is False


def test_prior_sar_blocks_auto_close() -> None:
    v = Verdict(
        disposition="close", risk_score=0.1, confidence=0.95, evidence_ids=["t:1"]
    )
    assert route_verdict(v, True, 0.3, 0.8) == "close_case"
    assert route_verdict(v, True, 0.3, 0.8, prior_sar=True) == "draft_sar"


def test_history_appears_in_prompts_only_when_present() -> None:
    alert = {"alert_id": 1, "rule_id": "r", "account_id": "A"}
    hist = [{"alert_id": 9, "analyst_notes": "payroll confirmed"}]
    assert "payroll confirmed" in prompts.plan_user(alert, None, hist)
    assert "payroll confirmed" in prompts.assess_user(alert, None, [], [], hist)
    assert "PRIOR HISTORY" not in prompts.plan_user(alert, None, [])
    assert "PRIOR HISTORY" not in prompts.assess_user(alert, None, [], [])
    assert "not citable evidence" in prompts.assess_user(alert, None, [], [], hist)

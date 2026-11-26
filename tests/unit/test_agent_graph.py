"""F06 unit tests: routing, grounding, tool-loop cap, schemas. No DB, no network."""

from contextlib import nullcontext
from typing import Any

import pytest
from langchain_core.tools import StructuredTool
from pydantic import ValidationError

from app.agent.evidence import build_evidence
from app.agent.llm import FakeLLM
from app.agent.nodes import ALERT_SQL, KYC_SQL, Nodes
from app.agent.routing import check_grounding, route_verdict
from app.agent.schemas import InvestigationPlan, PlannedCheck, SarDraft, Verdict
from app.config import Settings


def _verdict(**kw: Any) -> Verdict:
    base: dict[str, Any] = {
        "disposition": "close",
        "risk_score": 0.1,
        "confidence": 0.9,
        "evidence_ids": ["t:1"],
    }
    return Verdict(**{**base, **kw})


EVIDENCE: list[dict[str, Any]] = [
    {"evidence_id": "t:1", "status": "ok"},
    {"evidence_id": None},
]


def test_verdict_bounds_and_extra_forbidden() -> None:
    with pytest.raises(ValidationError):
        _verdict(risk_score=1.5)
    with pytest.raises(ValidationError):
        _verdict(confidence=-0.1)
    with pytest.raises(ValidationError):
        _verdict(disposition="maybe")
    with pytest.raises(ValidationError):
        Verdict(
            disposition="close",
            risk_score=0.1,
            confidence=0.9,
            evidence_ids=[],
            surprise=1,  # type: ignore[call-arg]
        )


def test_grounding_accepts_known_ids() -> None:
    assert check_grounding(_verdict(), EVIDENCE) == []


def test_grounding_rejects_unknown_and_empty() -> None:
    assert check_grounding(_verdict(evidence_ids=["t:1", "x:9"]), EVIDENCE) == [
        "unknown evidence id: x:9"
    ]
    assert check_grounding(_verdict(evidence_ids=[]), EVIDENCE) == [
        "verdict cites no evidence"
    ]


@pytest.mark.parametrize(
    ("kw", "grounded", "expected"),
    [
        ({}, True, "close_case"),
        ({"risk_score": 0.3}, True, "close_case"),  # boundary is inclusive
        ({"risk_score": 0.31}, True, "draft_sar"),
        ({"confidence": 0.79}, True, "draft_sar"),
        ({"confidence": 0.8}, True, "close_case"),
        ({"disposition": "escalate", "risk_score": 0.0}, True, "draft_sar"),
        ({}, False, "draft_sar"),  # ungrounded never auto-closes
    ],
)
def test_routing(kw: dict[str, Any], grounded: bool, expected: str) -> None:
    assert route_verdict(_verdict(**kw), grounded, 0.30, 0.80) == expected


def test_routing_without_verdict_escalates() -> None:
    assert route_verdict(None, True, 0.3, 0.8) == "draft_sar"


def test_sql_never_selects_ground_truth() -> None:
    for sql in (ALERT_SQL, KYC_SQL):
        assert "is_true_positive" not in sql and "typology" not in sql
        assert "*" not in sql


# ----------------------------------------------------------------- tool loop


def _fake_tool(name: str, calls: list[dict[str, Any]]) -> StructuredTool:
    async def run(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        n = kwargs.get("sample_size", 0)
        ev = build_evidence(name, kwargs, "ok", f"{name} ran", {"n": n})
        return ev.model_dump(mode="json")

    return StructuredTool.from_function(
        coroutine=run,
        name=name,
        description="fake",
        args_schema={"type": "object", "properties": {}},
    )


def _nodes(calls: list[dict[str, Any]], max_calls: int = 8) -> Nodes:
    settings = Settings(postgres_password="x", max_tool_calls=max_calls)  # type: ignore[call-arg]
    return Nodes(
        lambda: nullcontext(),  # type: ignore[arg-type,return-value]
        [
            _fake_tool("query_transactions", calls),
            _fake_tool("sanctions_screen", calls),
        ],
        FakeLLM(),
        FakeLLM(),
        settings,
    )


def _state(checks: list[PlannedCheck]) -> Any:
    return {
        "alert": {"account_id": 7},
        "plan": [c.model_dump() for c in checks],
        "evidence": [],
        "tool_calls": 0,
        "skipped_checks": [],
    }


async def test_tool_loop_stops_at_cap() -> None:
    calls: list[dict[str, Any]] = []
    checks = [
        PlannedCheck(tool="query_transactions", args={"sample_size": i + 1})
        for i in range(12)
    ]
    out = await _nodes(calls).tool_loop(_state(checks))
    assert len(calls) == 8 and out["tool_calls"] == 8
    assert len(out["evidence"]) == 8
    assert len(out["skipped_checks"]) == 4
    assert all("call cap reached" in s for s in out["skipped_checks"])


async def test_tool_loop_defaults_account_and_dedupes_and_drops_unknown_tools() -> None:
    calls: list[dict[str, Any]] = []
    checks = [
        PlannedCheck(tool="query_transactions"),
        PlannedCheck(tool="query_transactions"),  # identical => same evidence id
        PlannedCheck(tool="rm_rf"),
        PlannedCheck(tool="sanctions_screen", args={"account_id": 99}),
    ]
    out = await _nodes(calls).tool_loop(_state(checks))
    assert calls[0] == {"account_id": 7}
    assert calls[-1] == {"account_id": 99}  # explicit args win
    assert len(out["evidence"]) == 2  # duplicate collapsed
    assert out["skipped_checks"] == ["rm_rf: unknown tool"]


async def test_tool_loop_keeps_error_evidence_but_it_is_not_citable() -> None:
    def bad(**kwargs: Any) -> dict[str, Any]:
        return build_evidence(
            "query_transactions", kwargs, "error", "bad args"
        ).model_dump(mode="json")

    tool = StructuredTool.from_function(
        func=bad,
        name="query_transactions",
        description="x",
        args_schema={"type": "object"},
    )
    settings = Settings(postgres_password="x")  # type: ignore[call-arg]
    nodes = Nodes(lambda: nullcontext(), [tool], FakeLLM(), FakeLLM(), settings)  # type: ignore[arg-type,return-value]
    out = await nodes.tool_loop(_state([PlannedCheck(tool="query_transactions")]))
    assert out["evidence"][0]["status"] == "error"
    assert out["evidence"][0]["evidence_id"] is None
    assert check_grounding(
        _verdict(evidence_ids=["query_transactions:none"]), out["evidence"]
    )


# --------------------------------------------------------------- draft_sar


async def test_draft_sar_falls_back_when_narrative_cites_unknown_ids() -> None:
    ev = build_evidence("query_transactions", {"a": 1}, "ok", "10 txns").model_dump(
        mode="json"
    )
    llm = FakeLLM(SarDraft(narrative="made up", evidence_ids=["nope:1"]))
    settings = Settings(postgres_password="x")  # type: ignore[call-arg]
    nodes = Nodes(lambda: nullcontext(), [], llm, llm, settings)  # type: ignore[arg-type,return-value]
    out = await nodes.draft_sar(
        {
            "alert": {"alert_id": 1, "rule_id": "fan_out", "account_id": 2},
            "evidence": [ev],
            "verdict": _verdict(evidence_ids=[ev["evidence_id"]]).model_dump(),
            "grounded": True,
        }
    )
    assert out["outcome"] == "escalated"
    assert "made up" not in (out["sar_draft"] or {})["narrative"]
    assert ev["evidence_id"] in (out["sar_draft"] or {})["narrative"]


async def test_draft_sar_skips_llm_when_ungrounded() -> None:
    llm = FakeLLM()  # any call would raise
    settings = Settings(postgres_password="x")  # type: ignore[call-arg]
    nodes = Nodes(lambda: nullcontext(), [], llm, llm, settings)  # type: ignore[arg-type,return-value]
    out = await nodes.draft_sar(
        {
            "alert": {"alert_id": 1, "rule_id": "r", "account_id": 2},
            "evidence": [],
            "verdict": _verdict().model_dump(),
            "grounded": False,
            "grounding_errors": ["x"],
        }
    )
    assert out["outcome"] == "escalated"
    assert "could not be grounded" in (out["sar_draft"] or {})["narrative"]


async def test_fake_llm_rejects_wrong_schema() -> None:
    llm = FakeLLM(InvestigationPlan())
    with pytest.raises(TypeError):
        await llm.structured(Verdict, [])


# ------------------------------------------------------- scale context / prompts


def test_alert_context_scale_is_computed_in_python() -> None:
    from app.agent.routing import alert_context

    s = Settings(postgres_password="x")  # type: ignore[call-arg]
    ctx = alert_context(
        {"rule_id": "high_velocity", "trigger_txn_ids": list(range(50))}, s
    )
    assert ctx == {
        "trigger_txn_count": 50,
        "rule_threshold": "10 transactions",
        "times_threshold": 5.0,
    }
    # Rules without a simple count threshold only get the count.
    assert alert_context({"rule_id": "round_trip", "trigger_txn_ids": [1, 2]}, s) == {
        "trigger_txn_count": 2
    }


def test_prompt_view_drops_long_id_lists() -> None:
    from app.agent.prompts import alert_view, assess_user

    alert = {
        "alert_id": 1,
        "trigger_txn_ids": list(range(3000)),
        "trigger_txn_count": 3000,
    }
    view = alert_view(alert)
    assert "trigger_txn_ids" not in view and view["trigger_txn_sample"] == [
        0,
        1,
        2,
        3,
        4,
    ]
    assert len(assess_user(alert, None, [], [])) < 2000


# ------------------------------------------------------------ no-auto-close rules


NO_CLOSE = frozenset({"fan_in", "high_risk_counterparty"})


@pytest.mark.parametrize(
    ("rule", "expected"),
    [
        ("fan_in", "draft_sar"),
        ("high_risk_counterparty", "draft_sar"),
        ("fan_out", "close_case"),
        ("", "close_case"),
    ],
)
def test_rules_without_reliable_signal_never_auto_close(
    rule: str, expected: str
) -> None:
    got = route_verdict(_verdict(), True, 0.30, 0.80, rule, NO_CLOSE)
    assert got == expected


def test_escalation_reason_is_none_only_when_closing() -> None:
    from app.agent.routing import escalation_reason

    assert escalation_reason(_verdict(), True, 0.3, 0.8, "fan_out", NO_CLOSE) is None
    assert "never auto-closed" in str(
        escalation_reason(_verdict(), True, 0.3, 0.8, "fan_in", NO_CLOSE)
    )
    assert "not grounded" in str(escalation_reason(_verdict(), False, 0.3, 0.8))
    assert "risk score" in str(
        escalation_reason(_verdict(risk_score=0.5), True, 0.3, 0.8)
    )


def test_settings_parse_no_auto_close_rules() -> None:
    s = Settings(postgres_password="x", no_auto_close_rules=" fan_in , ,round_trip")  # type: ignore[call-arg]
    assert s.no_auto_close_rule_set == {"fan_in", "round_trip"}
    assert (
        Settings(postgres_password="x", no_auto_close_rules="").no_auto_close_rule_set
        == frozenset()
    )  # type: ignore[call-arg]


async def test_policy_forced_escalation_does_not_ask_llm_for_a_sar() -> None:
    llm = FakeLLM()  # any call would raise
    settings = Settings(postgres_password="x")  # type: ignore[call-arg]
    nodes = Nodes(lambda: nullcontext(), [], llm, llm, settings)  # type: ignore[arg-type,return-value]
    out = await nodes.draft_sar(
        {
            "alert": {"alert_id": 1, "rule_id": "fan_in", "account_id": 2},
            "evidence": [],
            "verdict": _verdict(rationale="ordinary activity").model_dump(),
            "grounded": True,
        }
    )
    assert out["outcome"] == "escalated"
    assert "never auto-closed" in str(out["escalation_reason"])
    narrative = (out["sar_draft"] or {})["narrative"]
    assert "low risk" in narrative and "ordinary activity" in narrative


# ---- F08: human_review interrupt


def _review_graph() -> Any:
    from langgraph.checkpoint.memory import MemorySaver
    from langgraph.graph import END, START, StateGraph

    from app.agent.state import InvestigationState

    g = StateGraph(InvestigationState)
    g.add_node("human_review", _nodes([]).human_review)
    g.add_edge(START, "human_review")
    g.add_edge("human_review", END)
    return g.compile(checkpointer=MemorySaver())


_ESCALATED: Any = {
    "alert_id": 3,
    "verdict": {"disposition": "escalate"},
    "sar_draft": {"narrative": "n", "evidence_ids": ["t:1"]},
    "escalation_reason": "risk",
    "outcome": "escalated",
}


async def test_human_review_pauses_with_the_review_payload() -> None:
    graph, cfg = _review_graph(), {"configurable": {"thread_id": "t"}}
    state = await graph.ainvoke(_ESCALATED, cfg)
    (intr,) = state["__interrupt__"]
    assert intr.value["alert_id"] == 3 and intr.value["sar_draft"]["narrative"] == "n"
    assert "decision" not in state  # nothing decided yet
    assert (await graph.aget_state(cfg)).next == ("human_review",)


@pytest.mark.parametrize(
    ("decision", "outcome"), [("approve", "escalated"), ("reject", "closed")]
)
async def test_human_review_resume_sets_decision_and_outcome(
    decision: str, outcome: str
) -> None:
    from langgraph.types import Command

    graph, cfg = _review_graph(), {"configurable": {"thread_id": "t"}}
    await graph.ainvoke(_ESCALATED, cfg)
    resume = {"decision": decision, "analyst_id": "ana", "notes": "n"}
    state = await graph.ainvoke(Command(resume=resume), cfg)
    assert "__interrupt__" not in state
    assert state["decision"] == decision and state["outcome"] == outcome
    assert state["analyst_id"] == "ana" and state["analyst_notes"] == "n"


async def test_human_review_rejects_an_unknown_decision() -> None:
    from langgraph.types import Command

    graph, cfg = _review_graph(), {"configurable": {"thread_id": "t"}}
    await graph.ainvoke(_ESCALATED, cfg)
    bad: Command[Any] = Command(resume={"decision": "maybe", "analyst_id": "a"})
    with pytest.raises(ValueError, match="unknown decision"):
        await graph.ainvoke(bad, cfg)


# ------------------------------------------------- F12: budget exhausted -> human


class _OverBudget:
    async def structured(self, schema: Any, messages: Any) -> Any:
        from app.agent.gateway import CostBudgetExceeded

        raise CostBudgetExceeded("over")


def _budget_nodes() -> Nodes:
    settings = Settings(postgres_password="x")  # type: ignore[call-arg]
    return Nodes(lambda: nullcontext(), [], _OverBudget(), _OverBudget(), settings)  # type: ignore[arg-type,return-value]


async def test_budget_exceeded_in_plan_gives_empty_plan() -> None:
    out = await _budget_nodes().plan(
        {"alert": {"alert_id": 1, "account_id": 2}, "kyc": None, "history": []}  # type: ignore[typeddict-item]
    )
    assert out == {"plan": []}


async def test_budget_exceeded_in_assess_escalates_without_retry() -> None:
    nodes = _budget_nodes()
    state: Any = {"alert": {"alert_id": 1, "rule_id": "velocity", "account_id": 2}}
    state.update(await nodes.assess(state))
    assert state["verdict"] is None
    state.update(await nodes.validate_grounding(state))
    assert state["grounded"] is False
    from app.agent.nodes import MAX_GROUNDING_RETRIES

    assert state["grounding_retries"] > MAX_GROUNDING_RETRIES  # never re-assessed
    out = await nodes.draft_sar(state)
    assert out["outcome"] == "escalated"
    assert out["escalation_reason"] == "no verdict"

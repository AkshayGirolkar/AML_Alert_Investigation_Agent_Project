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
    def run(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        n = kwargs.get("sample_size", 0)
        ev = build_evidence(name, kwargs, "ok", f"{name} ran", {"n": n})
        return ev.model_dump(mode="json")

    return StructuredTool.from_function(
        func=run,
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


def test_tool_loop_stops_at_cap() -> None:
    calls: list[dict[str, Any]] = []
    checks = [
        PlannedCheck(tool="query_transactions", args={"sample_size": i + 1})
        for i in range(12)
    ]
    out = _nodes(calls).tool_loop(_state(checks))
    assert len(calls) == 8 and out["tool_calls"] == 8
    assert len(out["evidence"]) == 8
    assert len(out["skipped_checks"]) == 4
    assert all("call cap reached" in s for s in out["skipped_checks"])


def test_tool_loop_defaults_account_and_dedupes_and_drops_unknown_tools() -> None:
    calls: list[dict[str, Any]] = []
    checks = [
        PlannedCheck(tool="query_transactions"),
        PlannedCheck(tool="query_transactions"),  # identical => same evidence id
        PlannedCheck(tool="rm_rf"),
        PlannedCheck(tool="sanctions_screen", args={"account_id": 99}),
    ]
    out = _nodes(calls).tool_loop(_state(checks))
    assert calls[0] == {"account_id": 7}
    assert calls[-1] == {"account_id": 99}  # explicit args win
    assert len(out["evidence"]) == 2  # duplicate collapsed
    assert out["skipped_checks"] == ["rm_rf: unknown tool"]


def test_tool_loop_keeps_error_evidence_but_it_is_not_citable() -> None:
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
    out = nodes.tool_loop(_state([PlannedCheck(tool="query_transactions")]))
    assert out["evidence"][0]["status"] == "error"
    assert out["evidence"][0]["evidence_id"] is None
    assert check_grounding(
        _verdict(evidence_ids=["query_transactions:none"]), out["evidence"]
    )


# --------------------------------------------------------------- draft_sar


def test_draft_sar_falls_back_when_narrative_cites_unknown_ids() -> None:
    ev = build_evidence("query_transactions", {"a": 1}, "ok", "10 txns").model_dump(
        mode="json"
    )
    llm = FakeLLM(SarDraft(narrative="made up", evidence_ids=["nope:1"]))
    settings = Settings(postgres_password="x")  # type: ignore[call-arg]
    nodes = Nodes(lambda: nullcontext(), [], llm, llm, settings)  # type: ignore[arg-type,return-value]
    out = nodes.draft_sar(
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


def test_draft_sar_skips_llm_when_ungrounded() -> None:
    llm = FakeLLM()  # any call would raise
    settings = Settings(postgres_password="x")  # type: ignore[call-arg]
    nodes = Nodes(lambda: nullcontext(), [], llm, llm, settings)  # type: ignore[arg-type,return-value]
    out = nodes.draft_sar(
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


def test_fake_llm_rejects_wrong_schema() -> None:
    llm = FakeLLM(InvestigationPlan())
    with pytest.raises(TypeError):
        llm.structured(Verdict, [])

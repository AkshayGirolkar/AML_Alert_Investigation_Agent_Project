"""Pure decision logic for the graph: grounding and routing. No I/O, no LLM."""

from typing import Any, Literal

from app.agent.schemas import Verdict


def check_grounding(verdict: Verdict, evidence: list[dict[str, Any]]) -> list[str]:
    """Return a list of problems (empty = grounded).

    Every cited ID must exist in state. Failed tool calls have no ID, so they can never
    be cited. A verdict with no evidence at all is not grounded either.
    """
    known = {e["evidence_id"] for e in evidence if e.get("evidence_id")}
    problems = [
        f"unknown evidence id: {i}" for i in verdict.evidence_ids if i not in known
    ]
    if not verdict.evidence_ids:
        problems.append("verdict cites no evidence")
    return problems


def route_verdict(
    verdict: Verdict | None,
    grounded: bool,
    close_risk_max: float,
    close_confidence_min: float,
) -> Literal["close_case", "draft_sar"]:
    """Auto-close only when everything is clearly benign; otherwise escalate."""
    if (
        verdict is not None
        and grounded
        and verdict.disposition == "close"
        and verdict.risk_score <= close_risk_max
        and verdict.confidence >= close_confidence_min
    ):
        return "close_case"
    return "draft_sar"

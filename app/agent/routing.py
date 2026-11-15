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


def escalation_reason(
    verdict: Verdict | None,
    grounded: bool,
    close_risk_max: float,
    close_confidence_min: float,
    rule_id: str = "",
    no_auto_close: frozenset[str] = frozenset(),
) -> str | None:
    """Why the case may not be auto-closed, or None if it may. First reason wins."""
    if verdict is None:
        return "no verdict"
    if not grounded:
        return "verdict is not grounded in evidence"
    if rule_id in no_auto_close:
        return f"rule '{rule_id}' is never auto-closed (no reliable signal to close it)"
    if verdict.disposition != "close":
        return "verdict recommends escalation"
    if verdict.risk_score > close_risk_max:
        return f"risk score {verdict.risk_score:.2f} is above the close limit"
    if verdict.confidence < close_confidence_min:
        return f"confidence {verdict.confidence:.2f} is below the close minimum"
    return None


def route_verdict(
    verdict: Verdict | None,
    grounded: bool,
    close_risk_max: float,
    close_confidence_min: float,
    rule_id: str = "",
    no_auto_close: frozenset[str] = frozenset(),
) -> Literal["close_case", "draft_sar"]:
    """Auto-close only when everything is clearly benign; otherwise escalate."""
    reason = escalation_reason(
        verdict, grounded, close_risk_max, close_confidence_min, rule_id, no_auto_close
    )
    return "draft_sar" if reason else "close_case"


# rule -> (Settings attribute holding the rule's threshold, unit of that threshold)
RULE_THRESHOLDS = {
    "high_velocity": ("rule_velocity_min_txns", "transactions"),
    "fan_out": ("rule_fan_min_degree", "distinct counterparties"),
    "fan_in": ("rule_fan_min_degree", "distinct counterparties"),
    "structuring": ("rule_structuring_min_txns", "transactions"),
}


def alert_context(alert: dict[str, Any], settings: Any) -> dict[str, Any]:
    """Scale facts computed in Python (the LLM never does arithmetic).

    A rule hit alone is the base case; how far past the rule's threshold the alert went
    is the useful signal. `times_threshold` compares trigger transactions with the rule's
    threshold, so for the fan rules (threshold counts counterparties) it is approximate.
    """
    count = len(alert.get("trigger_txn_ids", []))
    ctx: dict[str, Any] = {"trigger_txn_count": count}
    entry = RULE_THRESHOLDS.get(alert["rule_id"])
    if entry:
        attr, unit = entry
        threshold = getattr(settings, attr)
        ctx.update(
            rule_threshold=f"{threshold} {unit}",
            times_threshold=round(count / threshold, 1) if threshold else None,
        )
    return ctx

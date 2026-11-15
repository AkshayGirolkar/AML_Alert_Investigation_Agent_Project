"""Prompts for the three LLM steps."""

import json
from typing import Any

PLAN_SYSTEM = """You are an AML investigator planning checks for one transaction-monitoring alert.
Choose up to {max_calls} checks from these tools and give each tool's arguments:
{tools}
Rules: every tool takes `account_id` (use the alert's account). Prefer a time window around
the alert window. Pick checks that can confirm or refute the rule that fired. Do not
compute numbers yourself; tools return them."""

ASSESS_SYSTEM = """You are an AML investigator deciding whether to close or escalate an alert.
Use ONLY the evidence provided. Cite every supporting record by its evidence_id in
`evidence_ids`; never invent an ID. Do not do arithmetic; quote numbers from the evidence.
Escalate when evidence points to laundering, sanctions or adverse media hits, or activity
far out of line with the KYC profile or peers. Close only when the evidence explains the
alert as benign. risk_score and confidence are between 0 and 1. If you can name the
laundering pattern (e.g. FAN-OUT, FAN-IN, CYCLE, SCATTER-GATHER, STACK, STRUCTURING)
set `typology`, otherwise null."""

SAR_SYSTEM = """Draft a short suspicious activity report narrative for an analyst to review.
Use ONLY the evidence provided, cite the evidence_ids you rely on, and do not do arithmetic."""


def _dump(obj: Any) -> str:
    return json.dumps(obj, indent=2, default=str)


def plan_user(alert: dict[str, Any], kyc: dict[str, Any] | None) -> str:
    return f"ALERT:\n{_dump(alert)}\n\nKYC PROFILE:\n{_dump(kyc)}"


def evidence_block(evidence: list[dict[str, Any]]) -> str:
    return _dump(
        [
            {k: e[k] for k in ("evidence_id", "tool", "status", "summary", "data")}
            for e in evidence
        ]
    )


def assess_user(
    alert: dict[str, Any],
    kyc: dict[str, Any] | None,
    evidence: list[dict[str, Any]],
    feedback: list[str],
) -> str:
    text = (
        f"ALERT:\n{_dump(alert)}\n\nKYC PROFILE:\n{_dump(kyc)}\n\n"
        f"EVIDENCE:\n{evidence_block(evidence)}"
    )
    if feedback:
        text += "\n\nYOUR PREVIOUS ANSWER WAS REJECTED:\n" + "\n".join(feedback)
        text += "\nCite only evidence_ids listed above."
    return text


def sar_user(
    alert: dict[str, Any],
    kyc: dict[str, Any] | None,
    verdict: dict[str, Any],
    evidence: list[dict[str, Any]],
) -> str:
    return (
        f"ALERT:\n{_dump(alert)}\n\nKYC PROFILE:\n{_dump(kyc)}\n\n"
        f"VERDICT:\n{_dump(verdict)}\n\nEVIDENCE:\n{evidence_block(evidence)}"
    )

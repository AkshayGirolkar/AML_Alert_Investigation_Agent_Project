"""Graph state. Everything is JSON-safe so F07 can checkpoint it unchanged."""

from typing import Any, Literal, TypedDict


class InvestigationState(TypedDict, total=False):
    alert_id: int
    alert: dict[str, Any]
    kyc: dict[str, Any] | None
    plan: list[dict[str, Any]]
    evidence: list[dict[str, Any]]  # Evidence.model_dump(mode="json"), de-duplicated
    tool_calls: int  # calls made (incl. failed), capped at settings.max_tool_calls
    skipped_checks: list[str]  # planned checks dropped (unknown tool, over the cap)
    verdict: dict[str, Any] | None
    grounded: bool
    grounding_errors: list[str]
    grounding_retries: int
    outcome: Literal["closed", "escalated"]
    escalation_reason: str | None  # why it was not auto-closed (None when closed)
    sar_draft: dict[str, Any] | None

"""Case commands shared by the HTTP API and the console relay (F10.5).

Both entry points must apply the same rules (one case per alert, decisions only on
`awaiting_review`, one decision per case), so the logic lives here and each caller only
maps the outcome to its own response (HTTP status, or a `console.requests` result).
"""

from dataclasses import dataclass
from typing import Any, Literal

from app.db.cases import CaseRow, CaseStore, DecisionResult, DecisionRow
from app.messaging.sqs import InvestigationQueue

StartCode = Literal["accepted", "already_completed", "not_found"]


@dataclass(frozen=True)
class StartOutcome:
    code: StartCode
    case: CaseRow | None = None


@dataclass(frozen=True)
class DecideOutcome:
    result: DecisionResult
    decision: DecisionRow | None = None
    status: str | None = None


async def start_investigation(
    store: CaseStore, queue: InvestigationQueue, alert_id: int
) -> StartOutcome:
    """Create the case for this alert (once) and enqueue it. Safe to call repeatedly."""
    if not await store.alert_exists(alert_id):
        return StartOutcome("not_found")
    case, created = await store.create_or_get(alert_id)
    if case["status"] == "completed":
        return StartOutcome("already_completed", case)  # nothing to enqueue
    if case["status"] == "failed":
        case = await store.requeue(case["case_id"]) or case
    # The commit happened above; a failed send leaves a 'queued' case and a retry re-sends.
    if created or case["status"] == "queued":
        await queue.send(case["case_id"], alert_id)
    return StartOutcome("accepted", case)


async def record_decision(
    store: CaseStore,
    queue: InvestigationQueue,
    case_id: int,
    decision: str,
    analyst_id: str,
    notes: str | None,
    audit_extra: dict[str, Any] | None = None,
) -> DecideOutcome:
    """Record the analyst's decision, then ask the worker to resume the paused graph.

    A repeat of the same decision re-sends the resume message while the case is still
    waiting, in case the first send failed.
    """
    if audit_extra is None:
        result, row, status = await store.record_decision(
            case_id, decision, analyst_id, notes
        )
    else:
        result, row, status = await store.record_decision(
            case_id, decision, analyst_id, notes, audit_extra=audit_extra
        )
    if result in ("recorded", "duplicate") and status == "awaiting_review":
        case = await store.get(case_id)
        if case is not None:
            await queue.send(case_id, case["alert_id"], "resume")
    return DecideOutcome(result, row, status)

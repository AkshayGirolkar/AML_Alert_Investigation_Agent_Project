"""HTTP routes (F07, F08). Handlers depend on a `CaseStore` and an `InvestigationQueue`."""

from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from sqlalchemy import text

from app.api.schemas import DecisionRequest
from app.db.cases import CaseStore, public_case
from app.messaging.sqs import InvestigationQueue

router = APIRouter()


def _store(request: Request) -> CaseStore:
    return request.app.state.store  # type: ignore[no-any-return]


def _queue(request: Request) -> InvestigationQueue:
    return request.app.state.queue  # type: ignore[no-any-return]


@router.get("/health")
async def health(request: Request) -> dict[str, str]:
    engine = getattr(request.app.state, "engine", None)
    if engine is not None:
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        except Exception as exc:
            raise HTTPException(503, "database unavailable") from exc
    return {"status": "ok"}


@router.post("/alerts/{alert_id}/investigations", status_code=202)
async def start_investigation(
    alert_id: int, request: Request, response: Response
) -> dict[str, Any]:
    """Create the case for this alert (once) and enqueue it. Safe to call repeatedly."""
    store, queue = _store(request), _queue(request)
    if not await store.alert_exists(alert_id):
        raise HTTPException(404, f"alert {alert_id} not found")
    case, created = await store.create_or_get(alert_id)
    if case["status"] == "completed":
        response.status_code = 200  # already investigated; nothing to enqueue
        return public_case(case)
    if case["status"] == "failed":
        case = await store.requeue(case["case_id"]) or case
    # Commit happened above; a failed send leaves a 'queued' case and a retry re-sends.
    if created or case["status"] == "queued":
        await queue.send(case["case_id"], alert_id)
    return public_case(case)


@router.get("/cases/{case_id}")
async def get_case(case_id: int, request: Request) -> dict[str, Any]:
    case = await _store(request).get(case_id)
    if case is None:
        raise HTTPException(404, f"case {case_id} not found")
    out = public_case(case)
    out["decision"] = _decision_view(await _store(request).get_decision(case_id))
    return out


def _decision_view(row: dict[str, Any] | None) -> dict[str, Any] | None:
    return public_case(row) if row else None


@router.post("/cases/{case_id}/decision")
async def decide(
    case_id: int, body: DecisionRequest, request: Request, response: Response
) -> dict[str, Any]:
    """Analyst approves or rejects an escalated case; the worker then resumes the graph.

    Safe to repeat: the same decision again returns 200 (and re-sends the resume message
    if the case is still waiting, in case the first send failed).
    """
    store, queue = _store(request), _queue(request)
    result, decision, status = await store.record_decision(
        case_id, body.decision, body.analyst_id, body.notes
    )
    if result == "not_found":
        raise HTTPException(404, f"case {case_id} not found")
    if result == "not_reviewable":
        raise HTTPException(409, f"case {case_id} is {status}, not awaiting review")
    if result == "conflict":
        raise HTTPException(
            409,
            f"case {case_id} already has decision {decision['decision']!r}",  # type: ignore[index]
        )
    if result == "recorded" or status == "awaiting_review":
        case = await store.get(case_id)
        assert case is not None
        await queue.send(case_id, case["alert_id"], "resume")
    response.status_code = 202 if result == "recorded" else 200
    return {"case_id": case_id, "decision": _decision_view(decision), "status": status}


@router.get("/alerts/{alert_id}/case")
async def get_case_for_alert(alert_id: int, request: Request) -> dict[str, Any]:
    case = await _store(request).get_by_alert(alert_id)
    if case is None:
        raise HTTPException(404, f"no case for alert {alert_id}")
    return public_case(case)

"""HTTP routes (F07). Handlers depend on a `CaseStore` and an `InvestigationQueue`."""

from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from sqlalchemy import text

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
    return public_case(case)


@router.get("/alerts/{alert_id}/case")
async def get_case_for_alert(alert_id: int, request: Request) -> dict[str, Any]:
    case = await _store(request).get_by_alert(alert_id)
    if case is None:
        raise HTTPException(404, f"no case for alert {alert_id}")
    return public_case(case)

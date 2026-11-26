"""HTTP routes (F07, F08). Handlers depend on a `CaseStore` and an `InvestigationQueue`."""

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from redis.exceptions import RedisError
from sqlalchemy import text

from app.api.schemas import DecisionRequest
from app.config import get_settings
from app.db.cases import CaseStore, public_case
from app.messaging.sqs import InvestigationQueue
from app.observability.scrape import scrape
from app.services import case_commands as commands
from app.services.events import (
    TERMINAL_STATUSES,
    events_key,
    format_sse,
    parse_entry,
)

log = logging.getLogger("api")
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


@router.get("/ready")
async def ready(request: Request) -> dict[str, Any]:
    """Database is required (503 without it); Redis and SQS only mark the API degraded."""
    engine = getattr(request.app.state, "engine", None)
    if engine is not None:
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        except Exception as exc:
            raise HTTPException(503, "database unavailable") from exc
    degraded: list[str] = []
    redis = getattr(request.app.state, "redis", None)
    if redis is not None:
        try:
            await redis.ping()
        except Exception:
            log.exception("ready: redis unavailable")
            degraded.append("redis")
    depths = getattr(_queue(request), "depths", None)
    if depths is not None:
        try:
            await depths()
        except Exception:
            log.exception("ready: sqs unavailable")
            degraded.append("sqs")
    return {"status": "degraded" if degraded else "ok", "degraded": degraded}


@router.get("/metrics")
async def metrics_endpoint(request: Request) -> Response:
    text_out = await scrape(
        getattr(request.app.state, "engine", None),
        getattr(_queue(request), "depths", None),
    )
    return Response(text_out, media_type="text/plain; version=0.0.4")


@router.post("/alerts/{alert_id}/investigations", status_code=202)
async def start_investigation(
    alert_id: int, request: Request, response: Response
) -> dict[str, Any]:
    """Create the case for this alert (once) and enqueue it. Safe to call repeatedly."""
    out = await commands.start_investigation(_store(request), _queue(request), alert_id)
    if out.code == "not_found":
        raise HTTPException(404, f"alert {alert_id} not found")
    assert out.case is not None
    if out.code == "already_completed":
        response.status_code = 200  # already investigated; nothing was enqueued
    return public_case(out.case)


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
    out = await commands.record_decision(
        _store(request),
        _queue(request),
        case_id,
        body.decision,
        body.analyst_id,
        body.notes,
    )
    if out.result == "not_found":
        raise HTTPException(404, f"case {case_id} not found")
    if out.result == "not_reviewable":
        raise HTTPException(409, f"case {case_id} is {out.status}, not awaiting review")
    if out.result == "conflict":
        raise HTTPException(
            409,
            f"case {case_id} already has decision {out.decision['decision']!r}",  # type: ignore[index]
        )
    response.status_code = 202 if out.result == "recorded" else 200
    return {
        "case_id": case_id,
        "decision": _decision_view(out.decision),
        "status": out.status,
    }


@router.get("/alerts/{alert_id}/case")
async def get_case_for_alert(alert_id: int, request: Request) -> dict[str, Any]:
    case = await _store(request).get_by_alert(alert_id)
    if case is None:
        raise HTTPException(404, f"no case for alert {alert_id}")
    return public_case(case)


SSE_KEEPALIVE_SECONDS = 15


def _block_ms() -> int:
    """XREAD block time: must stay below the Redis socket timeout or the read errors."""
    timeout_ms = int(get_settings().redis_socket_timeout * 1000)
    return max(100, min(1000, timeout_ms - 500))


@router.get("/cases/{case_id}/events")
async def case_events(
    case_id: int,
    request: Request,
    last_event_id: str | None = Header(default=None),
) -> StreamingResponse:
    """Server-sent progress events for a case (F10).

    Starts with the current status, replays stream entries after `Last-Event-ID`, then
    follows new ones. Ends once the case is completed, failed or awaiting review.
    """
    store, redis = _store(request), request.app.state.redis
    case = await store.get(case_id)
    if case is None:
        raise HTTPException(404, f"case {case_id} not found")

    async def stream() -> AsyncIterator[str]:
        yield format_sse(None, "status", {"status": case["status"]})
        if redis is None:
            return
        cursor = last_event_id or "0-0"
        block, last_beat = _block_ms(), asyncio.get_running_loop().time()
        while not await request.is_disconnected():
            try:
                rows = await redis.xread(
                    {events_key(case_id): cursor}, block=block, count=50
                )
            except (RedisError, OSError):
                yield format_sse(None, "error", {"detail": "event stream unavailable"})
                return
            if not rows:
                now = asyncio.get_running_loop().time()
                if now - last_beat >= SSE_KEEPALIVE_SECONDS:
                    last_beat = now
                    yield ": keepalive\n\n"
                    current = await store.get(case_id)
                    if current and current["status"] in TERMINAL_STATUSES:
                        return
                continue
            for _, entries in rows:
                for entry_id, fields in entries:
                    cursor = entry_id
                    ev = parse_entry(entry_id, fields)
                    yield format_sse(entry_id, ev["type"], ev["data"])
                    if ev["type"] == "status" and ev["data"].get("status") in {
                        "completed",
                        "failed",
                        "awaiting_review",
                    }:
                        return
            await asyncio.sleep(0)

    return StreamingResponse(stream(), media_type="text/event-stream")

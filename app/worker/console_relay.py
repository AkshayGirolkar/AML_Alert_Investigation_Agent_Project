"""Console relay (F10.5): turns `console.requests` rows into investigations and decisions.

The separate console only INSERTs request rows. This task claims them (`FOR UPDATE SKIP
LOCKED`, so several workers can run it), applies them through `case_commands` (the same
code as the API) and records the outcome on the row. Delivery is at-least-once; the
commands are idempotent, so re-applying a request after a crash changes nothing.
"""

import asyncio
import json
import logging
from typing import Any

import psycopg
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db.cases import CaseStore
from app.messaging.sqs import InvestigationQueue
from app.services import case_commands as commands

log = logging.getLogger(__name__)

CHANNEL = "console_requests"
_CLAIM = text(
    "SELECT request_id, kind, alert_id, case_id, payload, requested_by, attempts "
    "FROM console.requests WHERE status = 'pending' "
    "ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED"
)
_FINISH = text(
    "UPDATE console.requests SET status = :status, result = CAST(:result AS jsonb), "
    "processed_at = now() WHERE request_id = :id"
)
_BUMP = text(
    "UPDATE console.requests SET attempts = attempts + 1 WHERE request_id = :id "
    "RETURNING attempts"
)
_HTTP = {"not_found": 404, "not_reviewable": 409, "conflict": 409}


class ConsoleRelay:
    def __init__(
        self,
        engine: AsyncEngine,
        store: CaseStore,
        queue: InvestigationQueue,
        max_attempts: int = 5,
    ) -> None:
        self._engine = engine
        self._store = store
        self._queue = queue
        self._max_attempts = max_attempts

    async def process_one(self) -> bool:
        """Apply the oldest pending request. Returns False when none was pending."""
        async with self._engine.connect() as conn:
            trans = await conn.begin()
            row = (await conn.execute(_CLAIM)).mappings().first()
            if row is None:
                await trans.rollback()
                return False
            req = dict(row)
            try:
                status, result = await self._apply(req)
            except Exception as exc:
                # Unexpected (e.g. SQS down): keep the request pending for a retry, but
                # give up on one that keeps failing so it cannot block the queue.
                await trans.rollback()
                log.exception("console request %s failed", req["request_id"])
                await self._record_failure(req["request_id"], exc)
                return True
            await conn.execute(
                _FINISH,
                {
                    "status": status,
                    "result": json.dumps(result),
                    "id": req["request_id"],
                },
            )
            await trans.commit()
            return True

    async def _record_failure(self, request_id: Any, exc: Exception) -> None:
        async with self._engine.begin() as conn:
            attempts = (await conn.execute(_BUMP, {"id": request_id})).scalar_one()
            if attempts >= self._max_attempts:
                err = {
                    "code": 500,
                    "error": f"gave up after {attempts} attempts: {exc!r}",
                }
                await conn.execute(
                    _FINISH,
                    {"status": "rejected", "result": json.dumps(err), "id": request_id},
                )

    async def _apply(self, req: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        if req["kind"] == "investigate":
            start = await commands.start_investigation(
                self._store, self._queue, req["alert_id"]
            )
            if start.code == "not_found":
                return "rejected", {
                    "code": 404,
                    "error": f"alert {req['alert_id']} not found",
                }
            assert start.case is not None
            return "applied", {
                "case_id": start.case["case_id"],
                "case_status": start.case["status"],
            }

        payload = req["payload"] or {}
        decision = payload.get("decision")
        notes = payload.get("notes")
        if decision not in ("approve", "reject") or not (
            notes is None or isinstance(notes, str)
        ):
            return "rejected", {
                "code": 400,
                "error": "payload needs decision 'approve' or 'reject' and optional text notes",
            }
        out = await commands.record_decision(
            self._store,
            self._queue,
            req["case_id"],
            decision,
            req["requested_by"],
            notes,
            audit_extra={"via": "console", "request_id": str(req["request_id"])},
        )
        if out.result in _HTTP:
            return "rejected", {
                "code": _HTTP[out.result],
                "error": f"{out.result} (case status {out.status})",
            }
        return "applied", {"case_id": req["case_id"], "case_status": out.status}

    async def drain(self) -> int:
        n = 0
        while await self.process_one():
            n += 1
        return n

    async def run(
        self, listen_conninfo: str, poll_seconds: float, stop: asyncio.Event
    ) -> None:
        """Drain, then sleep until a NOTIFY or the poll interval; repeat until stopped.

        The poll covers lost notifications and requests that arrived while we were down.
        """
        while not stop.is_set():
            try:
                await self.drain()
            except Exception:
                log.exception("console relay drain failed; retrying")
            await self._wait(listen_conninfo, poll_seconds)

    @staticmethod
    async def _wait(listen_conninfo: str, poll_seconds: float) -> None:
        try:
            async with await psycopg.AsyncConnection.connect(
                listen_conninfo, autocommit=True
            ) as conn:
                await conn.execute(f"LISTEN {CHANNEL}")
                async for _ in conn.notifies(timeout=poll_seconds, stop_after=1):
                    return
        except Exception:
            log.warning("LISTEN unavailable; falling back to polling", exc_info=True)
            await asyncio.sleep(poll_seconds)

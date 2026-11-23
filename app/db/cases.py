"""Case persistence (F07). One case per alert; every write is idempotent.

`CaseStore` is the interface the API and worker use, so unit tests can swap in a fake.
`SqlCaseStore` implements it with SQLAlchemy 2.0 async Core on the `cases` table.
"""

from datetime import datetime
from typing import Any, Literal, Protocol

from sqlalchemy import and_, exists, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db.models import Alert, AuditLog, Case, Decision

CaseRow = dict[str, Any]
DecisionRow = dict[str, Any]
# record_decision outcomes: recorded (new), duplicate (same decision again),
# conflict (a different decision is already on file), not_reviewable (not awaiting
# review), not_found.
DecisionResult = Literal[
    "recorded", "duplicate", "conflict", "not_reviewable", "not_found"
]
_DECISION_COLS = ("case_id", "decision", "analyst_id", "notes", "decided_at")
_PUBLIC = (
    "case_id",
    "alert_id",
    "status",
    "attempts",
    "outcome",
    "verdict",
    "sar_draft",
    "escalation_reason",
    "error",
    "created_at",
    "updated_at",
    "completed_at",
)


class CaseStore(Protocol):
    async def alert_exists(self, alert_id: int) -> bool: ...

    async def create_or_get(self, alert_id: int) -> tuple[CaseRow, bool]: ...

    async def get(self, case_id: int) -> CaseRow | None: ...

    async def get_by_alert(self, alert_id: int) -> CaseRow | None: ...

    async def requeue(self, case_id: int) -> CaseRow | None: ...

    async def claim(self, case_id: int, stale_after: int) -> CaseRow | None: ...

    async def touch(self, case_id: int) -> None: ...

    async def complete(self, case_id: int, state: dict[str, Any]) -> None: ...

    async def record_error(
        self, case_id: int, error: str, failed: bool, retry_status: str = "queued"
    ) -> None: ...

    async def mark_awaiting_review(
        self, case_id: int, state: dict[str, Any]
    ) -> None: ...

    async def record_decision(
        self, case_id: int, decision: str, analyst_id: str, notes: str | None
    ) -> tuple[DecisionResult, DecisionRow | None, str | None]: ...

    async def get_decision(self, case_id: int) -> DecisionRow | None: ...

    async def claim_resume(self, case_id: int, stale_after: int) -> CaseRow | None: ...


class SqlCaseStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    @staticmethod
    def _row(mapping: Any) -> CaseRow:
        return {k: mapping[k] for k in _PUBLIC}

    async def alert_exists(self, alert_id: int) -> bool:
        async with self._engine.connect() as conn:
            row = await conn.execute(
                select(Alert.alert_id).where(Alert.alert_id == alert_id)
            )
            return row.first() is not None

    async def create_or_get(self, alert_id: int) -> tuple[CaseRow, bool]:
        """Insert the case if it does not exist. Returns (case, created)."""
        async with self._engine.begin() as conn:
            inserted = await conn.execute(
                pg_insert(Case)
                .values(alert_id=alert_id)
                .on_conflict_do_nothing(index_elements=[Case.alert_id])
                .returning(Case.case_id)
            )
            created = inserted.first() is not None
            row = (
                (await conn.execute(select(Case).where(Case.alert_id == alert_id)))
                .mappings()
                .one()
            )
            return self._row(row), created

    async def get(self, case_id: int) -> CaseRow | None:
        return await self._select(Case.case_id == case_id)

    async def get_by_alert(self, alert_id: int) -> CaseRow | None:
        return await self._select(Case.alert_id == alert_id)

    async def _select(self, where: Any) -> CaseRow | None:
        async with self._engine.connect() as conn:
            row = (await conn.execute(select(Case).where(where))).mappings().first()
            return self._row(row) if row else None

    async def requeue(self, case_id: int) -> CaseRow | None:
        """failed -> queued (attempts reset). Any other status is left unchanged."""
        async with self._engine.begin() as conn:
            row = (
                (
                    await conn.execute(
                        update(Case)
                        .where(Case.case_id == case_id, Case.status == "failed")
                        .values(
                            status="queued",
                            attempts=0,
                            error=None,
                            updated_at=func.now(),
                        )
                        .returning(*[getattr(Case, c) for c in _PUBLIC])
                    )
                )
                .mappings()
                .first()
            )
            return self._row(row) if row else None

    async def claim(self, case_id: int, stale_after: int) -> CaseRow | None:
        """Take a case for processing. None means: nothing to do, so the caller just
        acknowledges the message (case missing, completed, failed, or already being run
        by a live worker).

        A `running` case is only taken over when it has not been touched for
        `stale_after` seconds, i.e. its worker died. A duplicate message for a case that
        a live worker is running is therefore ignored instead of run twice.
        """
        runnable = or_(
            Case.status == "queued",
            and_(
                Case.status == "running",
                Case.updated_at
                < func.now() - func.make_interval(0, 0, 0, 0, 0, 0, stale_after),
            ),
        )
        async with self._engine.begin() as conn:
            row = (
                (
                    await conn.execute(
                        update(Case)
                        .where(Case.case_id == case_id, runnable)
                        .values(
                            status="running",
                            attempts=Case.attempts + 1,
                            updated_at=func.now(),
                        )
                        .returning(*[getattr(Case, c) for c in _PUBLIC])
                    )
                )
                .mappings()
                .first()
            )
            return self._row(row) if row else None

    async def touch(self, case_id: int) -> None:
        """Heartbeat: show a live worker still owns this running case."""
        async with self._engine.begin() as conn:
            await conn.execute(
                update(Case)
                .where(Case.case_id == case_id, Case.status == "running")
                .values(updated_at=func.now())
            )

    async def complete(self, case_id: int, state: dict[str, Any]) -> None:
        """Store the result. Only a `running` case is updated, so a redelivered message
        can never overwrite a finished or paused case."""
        async with self._engine.begin() as conn:
            await conn.execute(
                update(Case)
                .where(Case.case_id == case_id, Case.status == "running")
                .values(
                    status="completed",
                    outcome=state.get("outcome"),
                    verdict=state.get("verdict"),
                    sar_draft=state.get("sar_draft"),
                    escalation_reason=state.get("escalation_reason"),
                    error=None,
                    updated_at=func.now(),
                    completed_at=func.now(),
                )
            )

    async def record_error(
        self, case_id: int, error: str, failed: bool, retry_status: str = "queued"
    ) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                update(Case)
                .where(Case.case_id == case_id, Case.status != "completed")
                .values(
                    status="failed" if failed else retry_status,
                    error=error[:2000],
                    updated_at=func.now(),
                )
            )

    async def mark_awaiting_review(self, case_id: int, state: dict[str, Any]) -> None:
        """The graph paused at the analyst-review interrupt. Idempotent."""
        async with self._engine.begin() as conn:
            await conn.execute(
                update(Case)
                .where(Case.case_id == case_id, Case.status == "running")
                .values(
                    status="awaiting_review",
                    verdict=state.get("verdict"),
                    sar_draft=state.get("sar_draft"),
                    escalation_reason=state.get("escalation_reason"),
                    error=None,
                    updated_at=func.now(),
                )
            )

    async def record_decision(
        self, case_id: int, decision: str, analyst_id: str, notes: str | None
    ) -> tuple[DecisionResult, DecisionRow | None, str | None]:
        """Store the analyst's decision once (UNIQUE case_id) with an audit row.

        Returns (result, decision on file, case status). The case row is locked, so two
        concurrent decisions cannot both be recorded.
        """
        async with self._engine.begin() as conn:
            case = (
                (
                    await conn.execute(
                        select(Case).where(Case.case_id == case_id).with_for_update()
                    )
                )
                .mappings()
                .first()
            )
            if case is None:
                return "not_found", None, None
            existing = await self._decision(conn, case_id)
            if existing is not None:
                same = existing["decision"] == decision
                return ("duplicate" if same else "conflict"), existing, case["status"]
            if case["status"] != "awaiting_review":
                return "not_reviewable", None, case["status"]
            row = (
                (
                    await conn.execute(
                        pg_insert(Decision)
                        .values(
                            case_id=case_id,
                            decision=decision,
                            analyst_id=analyst_id,
                            notes=notes,
                        )
                        .returning(*[getattr(Decision, c) for c in _DECISION_COLS])
                    )
                )
                .mappings()
                .one()
            )
            await conn.execute(
                pg_insert(AuditLog).values(
                    case_id=case_id,
                    actor=analyst_id,
                    action=f"decision_{decision}",
                    detail={"decision": decision, "notes": notes},
                )
            )
            return "recorded", dict(row), case["status"]

    @staticmethod
    async def _decision(conn: Any, case_id: int) -> DecisionRow | None:
        row = (
            (
                await conn.execute(
                    select(*[getattr(Decision, c) for c in _DECISION_COLS]).where(
                        Decision.case_id == case_id
                    )
                )
            )
            .mappings()
            .first()
        )
        return dict(row) if row else None

    async def get_decision(self, case_id: int) -> DecisionRow | None:
        async with self._engine.connect() as conn:
            return await self._decision(conn, case_id)

    async def claim_resume(self, case_id: int, stale_after: int) -> CaseRow | None:
        """Take an `awaiting_review` case whose decision is on file, to resume its graph.
        A `running` case is only taken over when stale (its worker died mid-resume)."""
        runnable = or_(
            Case.status == "awaiting_review",
            and_(
                Case.status == "running",
                Case.updated_at
                < func.now() - func.make_interval(0, 0, 0, 0, 0, 0, stale_after),
            ),
        )
        async with self._engine.begin() as conn:
            row = (
                (
                    await conn.execute(
                        update(Case)
                        .where(
                            Case.case_id == case_id,
                            runnable,
                            exists().where(Decision.case_id == Case.case_id),
                        )
                        .values(
                            status="running",
                            attempts=Case.attempts + 1,
                            updated_at=func.now(),
                        )
                        .returning(*[getattr(Case, c) for c in _PUBLIC])
                    )
                )
                .mappings()
                .first()
            )
            return self._row(row) if row else None


def public_case(row: CaseRow) -> dict[str, Any]:
    """JSON-ready view for the API (datetimes -> ISO strings)."""
    return {
        k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in row.items()
    }

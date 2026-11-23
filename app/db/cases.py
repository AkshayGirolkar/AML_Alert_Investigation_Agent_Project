"""Case persistence (F07). One case per alert; every write is idempotent.

`CaseStore` is the interface the API and worker use, so unit tests can swap in a fake.
`SqlCaseStore` implements it with SQLAlchemy 2.0 async Core on the `cases` table.
"""

from datetime import datetime
from typing import Any, Protocol

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db.models import Alert, Case

CaseRow = dict[str, Any]
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

    async def record_error(self, case_id: int, error: str, failed: bool) -> None: ...


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
        """Store the result. Running it twice with the same state gives the same row."""
        async with self._engine.begin() as conn:
            await conn.execute(
                update(Case)
                .where(Case.case_id == case_id)
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

    async def record_error(self, case_id: int, error: str, failed: bool) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                update(Case)
                .where(Case.case_id == case_id, Case.status != "completed")
                .values(
                    status="failed" if failed else "queued",
                    error=error[:2000],
                    updated_at=func.now(),
                )
            )


def public_case(row: CaseRow) -> dict[str, Any]:
    """JSON-ready view for the API (datetimes -> ISO strings)."""
    return {
        k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in row.items()
    }

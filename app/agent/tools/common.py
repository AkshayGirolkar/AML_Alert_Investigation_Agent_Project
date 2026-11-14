"""Shared helpers for the F05 tools."""

from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal
from typing import Any, TypeVar

import psycopg
from psycopg.rows import dict_row
from pydantic import BaseModel, ValidationError

from app.agent.evidence import Evidence, build_evidence

A = TypeVar("A", bound=BaseModel)


def run_tool(
    tool: str,
    model: type[A],
    params: dict[str, Any],
    impl: Callable[[A], Evidence],
) -> Evidence:
    """Validate `params`; invalid input becomes an error Evidence (never an exception).

    Database errors are not caught: an outage must not turn into citable evidence.
    """
    try:
        args = model(**params)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or 'input'}: {e['msg']}"
            for e in exc.errors()
        )
        return build_evidence(tool, params, "error", f"Invalid arguments: {problems}")
    return impl(args)


def cursor(conn: psycopg.Connection) -> psycopg.Cursor[dict[str, Any]]:
    return conn.cursor(row_factory=dict_row)


def money(d: Decimal | None) -> str:
    """Decimal -> plain string with no exponent or trailing zeros ('100', '0.5')."""
    if d is None:
        return "0"
    return format(d.normalize(), "f")


def iso(v: datetime | date | None) -> str | None:
    return v.isoformat() if v is not None else None


def entity_of(
    cur: psycopg.Cursor[dict[str, Any]], account_id: int
) -> dict[str, Any] | None:
    """Account -> entity + KYC columns (kyc_* are None when the entity has no profile)."""
    cur.execute(
        """
        SELECT a.account_id, a.entity_id, e.entity_type,
               k.entity_id IS NOT NULL AS has_kyc,
               k.full_name, k.country, k.risk_rating, k.expected_monthly_volume
        FROM accounts a
        JOIN entities e USING (entity_id)
        LEFT JOIN kyc_profiles k USING (entity_id)
        WHERE a.account_id = %s""",
        (account_id,),
    )
    return cur.fetchone()


def window_sql(alias: str = "t") -> str:
    """Optional [start, end) bounds on `alias.ts`; pass %(start)s / %(end)s (may be None)."""
    return (
        f"(%(start)s::timestamp IS NULL OR {alias}.ts >= %(start)s::timestamp) "
        f"AND (%(end)s::timestamp IS NULL OR {alias}.ts < %(end)s::timestamp)"
    )


def window_text(start: datetime | None, end: datetime | None) -> str:
    if start and end:
        return f"between {start:%Y-%m-%d %H:%M} and {end:%Y-%m-%d %H:%M}"
    if start:
        return f"from {start:%Y-%m-%d %H:%M}"
    if end:
        return f"before {end:%Y-%m-%d %H:%M}"
    return "over all time"

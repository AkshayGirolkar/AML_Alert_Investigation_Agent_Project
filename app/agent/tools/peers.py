"""peer_comparison: how an entity's outgoing activity ranks against similar customers."""

from decimal import Decimal
from statistics import median
from typing import Any

import psycopg

from app.agent.evidence import Evidence, build_evidence
from app.agent.tools.common import (
    cursor,
    entity_of,
    money,
    run_tool,
    window_sql,
    window_text,
)
from app.agent.tools.schemas import PeerComparisonArgs

TOOL = "peer_comparison"

# Peers share entity type and country. The sample is deterministic (md5 order), so the
# same call always compares against the same peers. Customer-level, not account-level.
_STATS = f"""
WITH peers AS (
  SELECT e.entity_id FROM entities e JOIN kyc_profiles k USING (entity_id)
  WHERE e.entity_type = %(etype)s AND k.country = %(country)s AND e.entity_id <> %(eid)s
  ORDER BY md5(e.entity_id) LIMIT %(sample)s
), members AS (
  SELECT entity_id FROM peers UNION ALL SELECT %(eid)s::text
)
SELECT m.entity_id, m.entity_id = %(eid)s AS is_subject,
       count(t.txn_id) AS txn_count,
       count(DISTINCT t.to_account_id) AS counterparties,
       coalesce(sum(t.amount_paid), 0) AS total_out
FROM members m
LEFT JOIN accounts a ON a.entity_id = m.entity_id
LEFT JOIN transactions t ON t.from_account_id = a.account_id AND {window_sql()}
GROUP BY m.entity_id
"""


def peer_comparison(conn: psycopg.Connection, **params: Any) -> Evidence:
    return run_tool(TOOL, PeerComparisonArgs, params, lambda a: _run(conn, a))


def _rank(value: Decimal | int, peers: list[Decimal] | list[int]) -> float | None:
    """Mid-rank percentile: share of peers below the value, ties counting half."""
    if not peers:
        return None
    below = sum(1 for p in peers if p < value)
    equal = sum(1 for p in peers if p == value)
    return round((below + equal / 2) / len(peers), 3)


def _run(conn: psycopg.Connection, a: PeerComparisonArgs) -> Evidence:
    args = a.model_dump(mode="json")
    with cursor(conn) as cur:
        who = entity_of(cur, a.account_id)
        if who is None:
            return build_evidence(
                TOOL,
                args,
                "empty",
                f"Account {a.account_id} does not exist.",
                {"account_exists": False},
            )
        if not who["has_kyc"]:
            return build_evidence(
                TOOL,
                args,
                "empty",
                f"Account {a.account_id} has no KYC profile, so no peer group exists.",
                {"account_exists": True, "has_kyc": False},
            )
        cur.execute(
            _STATS,
            {
                "etype": who["entity_type"],
                "country": who["country"],
                "eid": who["entity_id"],
                "sample": a.peer_sample,
                "start": a.start,
                "end": a.end,
            },
        )
        rows = cur.fetchall()

    subject = next(r for r in rows if r["is_subject"])
    peers = [r for r in rows if not r["is_subject"]]
    metrics: dict[str, dict[str, Any]] = {}
    for key, label in (
        ("txn_count", "outgoing_txn_count"),
        ("counterparties", "outgoing_distinct_counterparties"),
        ("total_out", "outgoing_total"),
    ):
        vals = [p[key] for p in peers]
        is_money = key == "total_out"
        fmt = money if is_money else (lambda v: v)
        metrics[label] = {
            "subject": fmt(subject[key]),
            "peer_median": fmt(median(vals)) if vals else None,
            "peer_max": fmt(max(vals)) if vals else None,
            "percentile": _rank(subject[key], vals),
        }

    expected: Decimal = who["expected_monthly_volume"]
    ratio = (
        (subject["total_out"] / expected).quantize(Decimal("0.001"))
        if expected and expected > 0
        else None
    )
    window = window_text(a.start, a.end)
    pct = metrics["outgoing_txn_count"]["percentile"]
    summary = (
        f"Entity {who['entity_id']} ({who['entity_type']}, {who['country']}) made "
        f"{subject['txn_count']} outgoing transactions {window}; "
        + (
            f"that ranks at the {round(pct * 100)}th percentile of {len(peers)} peers."
            if pct is not None
            else "no peers with the same type and country were found."
        )
    )
    data = {
        "account_exists": True,
        "has_kyc": True,
        "entity_id": who["entity_id"],
        "peer_group": {
            "entity_type": who["entity_type"],
            "country": who["country"],
            "peer_count": len(peers),
            "sampled": len(peers) >= a.peer_sample,
        },
        "metrics": metrics,
        "expected_monthly_volume": money(expected),
        "outgoing_total_to_expected_monthly_ratio": (
            None if ratio is None else money(ratio)
        ),
        "note": "Totals sum all currencies (no FX), the same basis as expected_monthly_volume.",
    }
    return build_evidence(TOOL, args, "ok", summary, data)

"""query_transactions: aggregates and a sample of one account's transactions."""

from typing import Any

import psycopg

from app.agent.evidence import Evidence, build_evidence
from app.agent.tools.common import (
    cursor,
    iso,
    money,
    run_tool,
    window_sql,
    window_text,
)
from app.agent.tools.schemas import QueryTransactionsArgs

TOOL = "query_transactions"

_SIDE = {
    "out": "t.from_account_id = %(a)s",
    "in": "t.to_account_id = %(a)s",
    "both": "(t.from_account_id = %(a)s OR t.to_account_id = %(a)s)",
}


def query_transactions(conn: psycopg.Connection, **params: Any) -> Evidence:
    return run_tool(TOOL, QueryTransactionsArgs, params, lambda a: _run(conn, a))


def _run(conn: psycopg.Connection, a: QueryTransactionsArgs) -> Evidence:
    args = a.model_dump(mode="json")
    q = {"a": a.account_id, "start": a.start, "end": a.end, "n": a.sample_size}
    with cursor(conn) as cur:
        cur.execute("SELECT 1 FROM accounts WHERE account_id = %s", (a.account_id,))
        if cur.fetchone() is None:
            return build_evidence(
                TOOL,
                args,
                "empty",
                f"Account {a.account_id} does not exist.",
                {"account_exists": False},
            )

        base = (
            "WITH f AS (SELECT t.*, (t.from_account_id = %(a)s) AS is_out "
            f"FROM transactions t WHERE {_SIDE[a.direction]} AND {window_sql()}) "
        )
        cur.execute(
            base
            + """SELECT count(*) AS n, count(*) FILTER (WHERE is_out) AS n_out,
                        min(ts) AS first_ts, max(ts) AS last_ts,
                        count(DISTINCT CASE WHEN is_out THEN to_account_id
                                            ELSE from_account_id END) AS counterparties
                 FROM f""",
            q,
        )
        total = cur.fetchone()
        assert total is not None
        window = window_text(a.start, a.end)
        if total["n"] == 0:
            return build_evidence(
                TOOL,
                args,
                "empty",
                f"Account {a.account_id} has no {a.direction} transactions {window}.",
                {"account_exists": True, "transaction_count": 0},
            )

        cur.execute(
            base
            + """SELECT payment_currency AS currency,
                        CASE WHEN is_out THEN 'out' ELSE 'in' END AS direction,
                        count(*) AS txn_count, sum(amount_paid) AS total,
                        max(amount_paid) AS largest
                 FROM f GROUP BY 1, 2 ORDER BY 2, 3 DESC, 1""",
            q,
        )
        by_currency = [
            {
                "currency": r["currency"],
                "direction": r["direction"],
                "txn_count": r["txn_count"],
                "total": money(r["total"]),
                "largest": money(r["largest"]),
            }
            for r in cur.fetchall()
        ]
        cur.execute(
            base + "SELECT payment_format AS format, count(*) AS txn_count FROM f "
            "GROUP BY 1 ORDER BY 2 DESC, 1",
            q,
        )
        by_format = cur.fetchall()
        cur.execute(
            base
            + """SELECT txn_id, ts, from_account_id, to_account_id, amount_paid,
                        payment_currency, payment_format
                 FROM f ORDER BY ts DESC, txn_id DESC LIMIT %(n)s""",
            q,
        )
        sample = [
            {
                "txn_id": r["txn_id"],
                "ts": iso(r["ts"]),
                "from_account_id": r["from_account_id"],
                "to_account_id": r["to_account_id"],
                "amount": money(r["amount_paid"]),
                "currency": r["payment_currency"],
                "format": r["payment_format"],
            }
            for r in cur.fetchall()
        ]

    n_in = total["n"] - total["n_out"]
    summary = (
        f"Account {a.account_id} has {total['n']} transactions ({total['n_out']} out, "
        f"{n_in} in) {window} with {total['counterparties']} distinct counterparties."
    )
    data = {
        "account_exists": True,
        "direction": a.direction,
        "transaction_count": total["n"],
        "outgoing_count": total["n_out"],
        "incoming_count": n_in,
        "first_ts": iso(total["first_ts"]),
        "last_ts": iso(total["last_ts"]),
        "distinct_counterparties": total["counterparties"],
        "by_currency": by_currency,  # totals are per currency: no FX conversion exists
        "by_format": by_format,
        "sample_order": "latest first",
        "sample": sample,
    }
    return build_evidence(TOOL, args, "ok", summary, data)

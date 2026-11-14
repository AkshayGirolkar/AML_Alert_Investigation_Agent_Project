"""counterparty_network: who an account transacts with, up to 2 hops (recursive CTE)."""

from collections import defaultdict
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
from app.agent.tools.schemas import CounterpartyNetworkArgs

TOOL = "counterparty_network"
MAX_EDGES = 200
MAX_CYCLES = 5
MAX_CYCLE_LEN = 4

# Each node expands to its top-K counterparties by transaction count (LATERAL ... LIMIT),
# so a hub cannot blow up the walk. Edges are treated as undirected for reachability.
_WALK = f"""
WITH RECURSIVE walk(account_id, hop) AS (
  SELECT %(a)s::int, 0
  UNION
  SELECT nb.cp, w.hop + 1
  FROM walk w
  CROSS JOIN LATERAL (
    SELECT x.cp, count(*) AS n
    FROM (
      SELECT t.to_account_id AS cp FROM transactions t
      WHERE t.from_account_id = w.account_id AND t.to_account_id <> w.account_id
        AND {window_sql()}
      UNION ALL
      SELECT t.from_account_id FROM transactions t
      WHERE t.to_account_id = w.account_id AND t.from_account_id <> w.account_id
        AND {window_sql()}
    ) x
    GROUP BY x.cp ORDER BY n DESC, x.cp LIMIT %(k)s
  ) nb
  WHERE w.hop < %(hops)s
)
SELECT account_id, min(hop) AS hop FROM walk GROUP BY account_id
"""

_ROOT_DEGREE = f"""
SELECT count(DISTINCT cp) AS degree FROM (
  SELECT t.to_account_id AS cp FROM transactions t
  WHERE t.from_account_id = %(a)s AND t.to_account_id <> %(a)s AND {window_sql()}
  UNION ALL
  SELECT t.from_account_id FROM transactions t
  WHERE t.to_account_id = %(a)s AND t.from_account_id <> %(a)s AND {window_sql()}
) x
"""


def counterparty_network(conn: psycopg.Connection, **params: Any) -> Evidence:
    return run_tool(TOOL, CounterpartyNetworkArgs, params, lambda a: _run(conn, a))


def _cycles_through(
    root: int, edges: set[tuple[int, int]], max_len: int, cap: int
) -> list[list[int]]:
    """Simple directed cycles through `root` (as node lists), shortest first."""
    out_nbrs: dict[int, list[int]] = defaultdict(list)
    for s, d in sorted(edges):
        out_nbrs[s].append(d)
    found: list[list[int]] = []

    def dfs(path: list[int]) -> None:
        if len(found) >= cap:
            return
        for nxt in out_nbrs[path[-1]]:
            if nxt == root and len(path) >= 2:
                found.append(list(path))
            elif nxt not in path and len(path) < max_len:
                dfs([*path, nxt])

    dfs([root])
    return sorted(found, key=lambda c: (len(c), c))[:cap]


def _run(conn: psycopg.Connection, a: CounterpartyNetworkArgs) -> Evidence:
    args = a.model_dump(mode="json")
    q = {"a": a.account_id, "start": a.start, "end": a.end}
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
        window = window_text(a.start, a.end)
        cur.execute(_ROOT_DEGREE, q)
        row = cur.fetchone()
        assert row is not None
        degree = row["degree"]
        if degree == 0:
            return build_evidence(
                TOOL,
                args,
                "empty",
                f"Account {a.account_id} has no counterparties {window}.",
                {"account_exists": True, "root_degree": 0},
            )

        cur.execute(_WALK, {**q, "k": a.max_neighbors, "hops": a.hops})
        hop_of = {r["account_id"]: r["hop"] for r in cur.fetchall()}
        ids = sorted(hop_of)

        cur.execute(
            """
            SELECT a.account_id, a.entity_id, e.entity_type, k.country, k.risk_rating
            FROM accounts a JOIN entities e USING (entity_id)
            LEFT JOIN kyc_profiles k USING (entity_id)
            WHERE a.account_id = ANY(%s)""",
            (ids,),
        )
        info = {r["account_id"]: r for r in cur.fetchall()}
        nodes = [
            {
                "account_id": i,
                "hop": hop_of[i],
                "entity_id": info[i]["entity_id"],
                "entity_type": info[i]["entity_type"],
                "country": info[i]["country"],
                "risk_rating": info[i]["risk_rating"],
            }
            for i in sorted(ids, key=lambda i: (hop_of[i], i))
        ]

        cur.execute(
            f"""
            SELECT t.from_account_id AS src, t.to_account_id AS dst,
                   t.payment_currency AS currency, count(*) AS txn_count,
                   sum(t.amount_paid) AS total, min(t.ts) AS first_ts, max(t.ts) AS last_ts
            FROM transactions t
            WHERE t.from_account_id = ANY(%(ids)s) AND t.to_account_id = ANY(%(ids)s)
              AND t.from_account_id <> t.to_account_id AND {window_sql()}
            GROUP BY 1, 2, 3
            ORDER BY txn_count DESC, 1, 2, 3
            LIMIT %(cap)s""",
            {**q, "ids": ids, "cap": MAX_EDGES + 1},
        )
        rows = cur.fetchall()

    edges_truncated = len(rows) > MAX_EDGES
    rows = rows[:MAX_EDGES]
    edges = [
        {
            "from": r["src"],
            "to": r["dst"],
            "currency": r["currency"],
            "txn_count": r["txn_count"],
            "total": money(r["total"]),
            "first_ts": iso(r["first_ts"]),
            "last_ts": iso(r["last_ts"]),
        }
        for r in rows
    ]
    cycles = _cycles_through(
        a.account_id, {(r["src"], r["dst"]) for r in rows}, MAX_CYCLE_LEN, MAX_CYCLES
    )
    n1 = sum(1 for n in nodes if n["hop"] == 1)
    n2 = sum(1 for n in nodes if n["hop"] == 2)
    summary = (
        f"Account {a.account_id} has {degree} distinct counterparties {window}; "
        f"the {a.hops}-hop network shows {n1} direct and {n2} second-hop accounts, "
        f"{len(edges)} edges and {len(cycles)} cycle(s) through the account."
    )
    data = {
        "account_exists": True,
        "root_degree": degree,
        "truncated": degree > a.max_neighbors,  # root had more counterparties than kept
        "edges_truncated": edges_truncated,
        "nodes": nodes,
        "edges": edges,
        "cycles_through_account": cycles,
    }
    return build_evidence(TOOL, args, "ok", summary, data)

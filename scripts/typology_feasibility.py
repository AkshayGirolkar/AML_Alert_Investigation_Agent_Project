"""Which IBM typologies can the agent's tools tell apart? (F19, free: no LLM calls)

.venv/bin/python -m scripts.typology_feasibility --dataset eval/datasets/tune-100.json

Runs query_transactions and counterparty_network with default arguments over each alert's
own window for the true alerts, then prints per-typology means of the structural signals
the model would see. Ground-truth typology is read here only to group the table; it never
reaches the agent. Read-only.
"""

import argparse
import asyncio
import json
import statistics
from collections import defaultdict
from typing import Any

import psycopg
from psycopg.rows import dict_row

from app.agent.tools.network import counterparty_network
from app.agent.tools.registry import make_conn_factory
from app.agent.tools.transactions import query_transactions
from app.config import get_settings
from app.db.copy import conninfo
from app.eval.dataset import EvalDataset
from app.eval.typology import UNTYPED

FEATURES = (
    "n_txn",
    "n_out",
    "n_in",
    "counterparties",
    "root_degree",
    "hop1",
    "hop2",
    "edges",
    "cycles",
)


async def _features(
    factory: Any, account_id: int, start: str, end: str
) -> dict[str, float]:
    async with factory() as conn:
        tx = (
            await query_transactions(conn, account_id=account_id, start=start, end=end)
        ).data
        net = (
            await counterparty_network(
                conn, account_id=account_id, start=start, end=end
            )
        ).data
    nodes = net.get("nodes", [])
    return {
        "n_txn": tx.get("transaction_count", 0),
        "n_out": tx.get("outgoing_count", 0),
        "n_in": tx.get("incoming_count", 0),
        "counterparties": tx.get("distinct_counterparties", 0),
        "root_degree": net.get("root_degree", 0),
        "hop1": sum(1 for n in nodes if n["hop"] == 1),
        "hop2": sum(1 for n in nodes if n["hop"] == 2),
        "edges": len(net.get("edges", [])),
        "cycles": len(net.get("cycles_through_account", [])),
    }


async def main_async(path: str) -> int:
    s = get_settings()
    ds = EvalDataset.load(path)
    factory = make_conn_factory(s.sync_database_url)
    ids = [i.alert_id for i in ds.items if i.truth]
    async with await psycopg.AsyncConnection.connect(
        conninfo(s.sync_database_url), row_factory=dict_row
    ) as conn:
        cur = await conn.execute(
            "SELECT alert_id, account_id, window_start, window_end FROM alerts "
            "WHERE alert_id = ANY(%s)",
            (ids,),
        )
        alerts = {r["alert_id"]: r for r in await cur.fetchall()}
    by_type: dict[str, list[dict[str, float]]] = defaultdict(list)
    for item in ds.items:
        if not item.truth:
            continue
        a = alerts[item.alert_id]
        feats = await _features(
            factory,
            a["account_id"],
            a["window_start"].isoformat(),
            a["window_end"].isoformat(),
        )
        by_type[item.truth_typology or UNTYPED].append(feats)

    print(f"{'typology':16}{'n':>3} " + " ".join(f"{f:>14}" for f in FEATURES))
    for name in sorted(by_type):
        rows = by_type[name]
        cells = []
        for f in FEATURES:
            vals = [r[f] for r in rows]
            cells.append(
                f"{statistics.median(vals):>6.0f}({min(vals):.0f}-{max(vals):.0f})".rjust(
                    14
                )
            )
        print(f"{name:16}{len(rows):>3} " + " ".join(cells))
    print("\ncells: median (min-max) over the true alerts of that typology")
    print(json.dumps({k: len(v) for k, v in by_type.items()}))
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dataset", default="eval/datasets/tune-100.json")
    raise SystemExit(asyncio.run(main_async(ap.parse_args().dataset)))

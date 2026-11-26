"""Load set for F16: which alerts a load test investigates, and how to reset them.

The set is deterministic (ordered by md5(seed:alert_id)) and frozen in a manifest, so
reruns send the same alerts. It never contains an eval-set alert or an alert that already
has a case. `reset` deletes only the rows of the manifest's alerts (cases, LLM usage,
decisions, audit log, checkpoints and the customer-memory records the worker wrote), and
refuses anything outside it.
"""

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
from sqlalchemy.engine import make_url

from app.db.copy import conninfo

LOCAL_HOSTS = frozenset(
    {"localhost", "127.0.0.1", "::1", "postgres", "host.docker.internal"}
)
MEMORY_PREFIX = "customer_history"

POOL_SQL = """
SELECT a.alert_id FROM alerts a
WHERE NOT EXISTS (SELECT 1 FROM cases c WHERE c.alert_id = a.alert_id)
"""


@dataclass(frozen=True)
class LoadSet:
    name: str
    seed: int
    alert_ids: list[int]
    excluded_eval: int  # how many eval-set alerts were kept out

    def to_json(self) -> str:
        data = {
            "name": self.name,
            "seed": self.seed,
            "count": len(self.alert_ids),
            "excluded_eval": self.excluded_eval,
            "alert_ids": self.alert_ids,
        }
        return json.dumps(data, indent=2) + "\n"

    def save(self, path: str | Path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(self.to_json())

    @classmethod
    def load(cls, path: str | Path) -> "LoadSet":
        d = json.loads(Path(path).read_text())
        return cls(d["name"], d["seed"], list(d["alert_ids"]), d["excluded_eval"])


def order_key(seed: int, alert_id: int) -> str:
    return hashlib.md5(f"{seed}:{alert_id}".encode()).hexdigest()


def select_alert_ids(
    pool: Sequence[int], excluded: set[int], count: int, seed: int
) -> list[int]:
    """The first `count` eligible ids in md5(seed:id) order. Raises if too few."""
    eligible = sorted(
        {a for a in pool if a not in excluded}, key=lambda a: (order_key(seed, a), a)
    )
    if len(eligible) < count:
        raise ValueError(
            f"only {len(eligible)} eligible alerts for a load set of {count}"
        )
    return eligible[:count]


def eval_alert_ids(path: str | Path) -> set[int]:
    p = Path(path)
    if not p.exists():
        return set()
    return {int(i["alert_id"]) for i in json.loads(p.read_text())["items"]}


def is_local(database_url: str) -> bool:
    host = make_url(database_url).host
    return host is None or host in LOCAL_HOSTS


def build_load_set(
    database_url: str, name: str, count: int, seed: int, eval_ids: set[int]
) -> LoadSet:
    with psycopg.connect(conninfo(database_url)) as conn:
        pool = [int(r[0]) for r in conn.execute(POOL_SQL).fetchall()]
    ids = select_alert_ids(pool, eval_ids, count, seed)
    return LoadSet(name, seed, ids, len(eval_ids & set(pool)))


def reset(
    database_url: str, alert_ids: Sequence[int], eval_ids: set[int]
) -> dict[str, int]:
    """Delete everything a load run created for these alerts. Returns rows per table."""
    ids = list(alert_ids)
    clash = eval_ids & set(ids)
    if clash:
        raise ValueError(f"refusing to reset eval-set alerts: {sorted(clash)[:5]}")
    threads = [f"alert-{a}" for a in ids]
    keys = [f"alert-{a}" for a in ids]
    counts: dict[str, int] = {}
    with psycopg.connect(conninfo(database_url)) as conn:
        case_ids: list[int] = [
            int(r[0])
            for r in conn.execute(
                "SELECT case_id FROM cases WHERE alert_id = ANY(%s)", (ids,)
            ).fetchall()
        ]
        steps: list[tuple[str, str, tuple[Any, ...]]] = [
            ("llm_calls", "DELETE FROM llm_calls WHERE case_id = ANY(%s)", (case_ids,)),
            ("decisions", "DELETE FROM decisions WHERE case_id = ANY(%s)", (case_ids,)),
            ("audit_log", "DELETE FROM audit_log WHERE case_id = ANY(%s)", (case_ids,)),
            ("cases", "DELETE FROM cases WHERE alert_id = ANY(%s)", (ids,)),
            (
                "checkpoint_writes",
                "DELETE FROM checkpoint_writes WHERE thread_id = ANY(%s)",
                (threads,),
            ),
            (
                "checkpoint_blobs",
                "DELETE FROM checkpoint_blobs WHERE thread_id = ANY(%s)",
                (threads,),
            ),
            (
                "checkpoints",
                "DELETE FROM checkpoints WHERE thread_id = ANY(%s)",
                (threads,),
            ),
            (
                "store",
                "DELETE FROM store WHERE key = ANY(%s) AND prefix LIKE %s",
                (keys, f"{MEMORY_PREFIX}%"),
            ),
        ]
        for table, sql, params in steps:
            # checkpoint/store tables are created by the worker on first start
            if conn.execute("SELECT to_regclass(%s)", (table,)).fetchone() == (None,):
                counts[table] = 0
                continue
            counts[table] = conn.execute(sql, params).rowcount
        conn.commit()
    return counts

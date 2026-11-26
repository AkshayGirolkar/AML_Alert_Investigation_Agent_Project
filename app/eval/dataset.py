"""Frozen, deterministic eval set of labeled alerts (F15).

Selection is stratified: about half true alerts spread evenly over the true typologies,
the rest false alerts spread evenly over the rules. Within a stratum alerts are ordered
by md5(seed:alert_id), so the same database and seed always give the same file. The file
(`eval/datasets/<name>.json`) is committed so every run and PR scores the same alerts.
Labels are stored here only for the scorer; the agent never sees them.
"""

import hashlib
import json
from collections import defaultdict
from collections.abc import Collection, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import psycopg

from app.db.copy import conninfo
from app.eval.typology import UNTYPED


@dataclass(frozen=True)
class Candidate:
    alert_id: int
    rule_id: str
    truth: bool
    truth_typology: str | None


@dataclass(frozen=True)
class EvalDataset:
    name: str
    seed: int
    size: int
    strata: dict[str, int]  # "true:CYCLE" / "false:fan_out" -> alerts selected
    items: list[Candidate]

    def to_json(self) -> str:
        data: dict[str, Any] = {
            "name": self.name,
            "seed": self.seed,
            "size": self.size,
            "strata": dict(sorted(self.strata.items())),
            "items": [asdict(i) for i in self.items],
        }
        return json.dumps(data, indent=2, sort_keys=True) + "\n"

    def save(self, path: str | Path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(self.to_json())

    @classmethod
    def load(cls, path: str | Path) -> "EvalDataset":
        d = json.loads(Path(path).read_text())
        return cls(
            d["name"],
            d["seed"],
            d["size"],
            d["strata"],
            [Candidate(**i) for i in d["items"]],
        )


def order_key(seed: int, alert_id: int) -> str:
    return hashlib.md5(f"{seed}:{alert_id}".encode()).hexdigest()


def _round_robin(
    groups: dict[str, list[Candidate]], target: int, seed: int
) -> list[tuple[str, Candidate]]:
    """Take one alert per group in turn (groups sorted by name) until target is met."""
    queues = {
        g: sorted(items, key=lambda c: order_key(seed, c.alert_id))
        for g, items in sorted(groups.items())
    }
    picked: list[tuple[str, Candidate]] = []
    while len(picked) < target and any(queues.values()):
        for g, q in queues.items():
            if q and len(picked) < target:
                picked.append((g, q.pop(0)))
    return picked


def select_items(
    candidates: Sequence[Candidate], size: int, seed: int
) -> tuple[list[Candidate], dict[str, int]]:
    """Pure selection. If an arm runs out, the other arm fills the shortfall."""
    true_groups: dict[str, list[Candidate]] = defaultdict(list)
    false_groups: dict[str, list[Candidate]] = defaultdict(list)
    for c in candidates:
        if c.truth:
            true_groups[c.truth_typology or UNTYPED].append(c)
        else:
            false_groups[c.rule_id].append(c)

    true_pick = _round_robin(true_groups, size - size // 2, seed)
    false_pick = _round_robin(false_groups, size - len(true_pick), seed)
    if len(true_pick) + len(false_pick) < size:  # not enough false: top up with true
        have = {c.alert_id for _, c in true_pick}
        rest = {
            g: [c for c in items if c.alert_id not in have]
            for g, items in true_groups.items()
        }
        true_pick += _round_robin(rest, size - len(true_pick) - len(false_pick), seed)

    strata: dict[str, int] = defaultdict(int)
    for g, _ in true_pick:
        strata[f"true:{g}"] += 1
    for g, _ in false_pick:
        strata[f"false:{g}"] += 1
    items = sorted((c for _, c in true_pick + false_pick), key=lambda c: c.alert_id)
    return items, dict(strata)


# Per stratum, only the first `size` alerts in hash order can ever be picked.
CANDIDATE_SQL = """
SELECT alert_id, rule_id, is_true_positive, typology FROM (
    SELECT alert_id, rule_id, is_true_positive, typology,
           row_number() OVER (
               PARTITION BY is_true_positive,
                            CASE WHEN is_true_positive THEN coalesce(typology, %(untyped)s)
                                 ELSE rule_id END
               ORDER BY md5(%(seed)s::text || ':' || alert_id::text)
           ) AS rn
    FROM alerts
    WHERE alert_id <> ALL(%(exclude)s::bigint[])
) t WHERE rn <= %(size)s
"""


def fetch_candidates(
    database_url: str, size: int, seed: int, exclude: Collection[int] = ()
) -> list[Candidate]:
    with psycopg.connect(conninfo(database_url)) as conn:
        rows = conn.execute(
            CANDIDATE_SQL,
            {
                "seed": seed,
                "size": size,
                "untyped": UNTYPED,
                "exclude": sorted(exclude),
            },
        ).fetchall()
    return [Candidate(int(a), r, bool(t), ty) for a, r, t, ty in rows]


def build_dataset(
    database_url: str,
    size: int,
    seed: int,
    name: str = "eval",
    exclude: Collection[int] = (),
) -> EvalDataset:
    """`exclude`: alert ids that must not appear (e.g. the confirmation set, F19)."""
    items, strata = select_items(
        fetch_candidates(database_url, size, seed, exclude), size, seed
    )
    return EvalDataset(name, seed, size, strata, items)

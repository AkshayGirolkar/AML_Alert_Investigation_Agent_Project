"""Persist eval runs: Postgres (`eval_runs`, `eval_results`) and a JSON file (F15).

Rows are keyed by (run_id, alert_id) and upserted, so a run can be resumed and a
rerun of the same alert replaces its row. Eval never writes `cases`, `decisions` or
`audit_log`.
"""

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from app.db.copy import conninfo
from app.eval.metrics import ResultRow


@dataclass(frozen=True)
class RunInfo:
    run_id: str
    dataset: str
    planner_model: str
    assess_model: str
    git_sha: str | None = None
    config: dict[str, Any] = field(default_factory=dict)


ROW_COLUMNS = (
    "alert_id, rule_id, truth, truth_typology, outcome, typology, risk, confidence, "
    "grounded, n_citations, tool_calls, cost_usd, seconds, error, budget_exceeded, "
    "rationale, node_costs"
)
_FIELDS = [c.strip() for c in ROW_COLUMNS.split(",")]


def _values(row: ResultRow) -> list[Any]:
    d = asdict(row)
    d["node_costs"] = Jsonb(d["node_costs"])
    return [d[f] for f in _FIELDS]


def save_run(database_url: str, info: RunInfo) -> None:
    with psycopg.connect(conninfo(database_url)) as conn:
        conn.execute(
            "INSERT INTO eval_runs (run_id, dataset, git_sha, planner_model, "
            "assess_model, config) VALUES (%s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (run_id) DO NOTHING",
            (
                info.run_id,
                info.dataset,
                info.git_sha,
                info.planner_model,
                info.assess_model,
                Jsonb(info.config),
            ),
        )


def save_result(database_url: str, run_id: str, row: ResultRow) -> None:
    placeholders = ", ".join(["%s"] * (len(_FIELDS) + 1))
    updates = ", ".join(f"{f} = EXCLUDED.{f}" for f in _FIELDS if f != "alert_id")
    with psycopg.connect(conninfo(database_url)) as conn:
        conn.execute(
            f"INSERT INTO eval_results (run_id, {ROW_COLUMNS}) VALUES ({placeholders}) "
            f"ON CONFLICT (run_id, alert_id) DO UPDATE SET {updates}",
            [run_id, *_values(row)],
        )


def finish_run(database_url: str, run_id: str) -> None:
    with psycopg.connect(conninfo(database_url)) as conn:
        conn.execute(
            "UPDATE eval_runs SET finished_at = now() WHERE run_id = %s", (run_id,)
        )


def load_results(database_url: str, run_id: str) -> list[ResultRow]:
    with psycopg.connect(conninfo(database_url)) as conn:
        rows = conn.execute(
            f"SELECT {ROW_COLUMNS} FROM eval_results WHERE run_id = %s "
            "ORDER BY alert_id",
            (run_id,),
        ).fetchall()
    return [ResultRow(**dict(zip(_FIELDS, r, strict=True))) for r in rows]


def load_info(database_url: str, run_id: str) -> RunInfo | None:
    with psycopg.connect(conninfo(database_url)) as conn:
        r = conn.execute(
            "SELECT run_id, dataset, planner_model, assess_model, git_sha, config "
            "FROM eval_runs WHERE run_id = %s",
            (run_id,),
        ).fetchone()
    return RunInfo(r[0], r[1], r[2], r[3], r[4], r[5]) if r else None


# ---- JSON files: eval/runs/<run_id>.json and the committed replay fixture


def write_run_file(path: str | Path, info: RunInfo, rows: list[ResultRow]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    data = {"info": asdict(info), "rows": [asdict(r) for r in rows]}
    p.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def read_run_file(path: str | Path) -> tuple[RunInfo, list[ResultRow]]:
    d = json.loads(Path(path).read_text())
    return RunInfo(**d["info"]), [ResultRow(**r) for r in d["rows"]]

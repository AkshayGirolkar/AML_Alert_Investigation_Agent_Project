"""Run the agent over an eval set, store the results and write a report (F15).

.venv/bin/python -m scripts.eval_run --dataset eval/datasets/eval-100.json --limit 10
Costs real OpenAI money (the gateway budget caps each case). Use --limit for a smoke run.
"""

import argparse
import asyncio
import dataclasses

from sqlalchemy.engine import make_url

from app.config import get_settings
from app.eval.dataset import EvalDataset
from app.eval.pipeline import default_run_id, execute_run, write_outputs
from app.eval.report import pct
from app.observability.logging import configure_logging


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dataset", default="eval/datasets/eval-100.json")
    p.add_argument("--limit", type=int, default=None, help="first N alerts only")
    p.add_argument("--run-id", default=None)
    p.add_argument("--planner-model", default=None)
    p.add_argument("--assess-model", default=None)
    p.add_argument("--concurrency", type=int, default=4)
    p.add_argument("--resume", action="store_true", help="skip alerts already scored")
    p.add_argument(
        "--record", default=None, help="also write the run to this JSON path"
    )
    p.add_argument("--langsmith", action="store_true", help="sync to LangSmith")
    a = p.parse_args()

    s = get_settings()
    overrides = {
        k: v
        for k, v in (
            ("llm_plan_model", a.planner_model),
            ("llm_assess_model", a.assess_model),
        )
        if v
    }
    if overrides:
        s = s.model_copy(update=overrides)
    configure_logging(s.log_format, s.log_level)
    ds = EvalDataset.load(a.dataset)
    if a.limit:
        ds = dataclasses.replace(ds, items=ds.items[: a.limit])
    run_id = a.run_id or default_run_id(ds.name, s.llm_plan_model, s.llm_assess_model)
    print(f"Target database: {make_url(s.sync_database_url).database}")
    print(f"Run {run_id}: {len(ds.items)} alerts, concurrency {a.concurrency}")

    scored = asyncio.run(execute_run(s, ds, run_id, a.concurrency, a.resume))
    run_file, report, chart = write_outputs(scored)
    if a.record:
        from app.eval.store import write_run_file

        write_run_file(a.record, scored.info, scored.rows)
    if a.langsmith:
        from langsmith import Client

        from app.eval.langsmith_sync import sync_run

        sync_run(Client(), ds, scored.info, scored.rows, scored.metrics)

    o = scored.metrics.overall
    print(
        f"recall {pct(o.recall)}  precision {pct(o.precision)}  "
        f"errors {pct(scored.metrics.error_rate)}  "
        f"mean cost ${scored.metrics.cost.mean or 0:.4f}"
    )
    print(f"Wrote {run_file}, {report}, {chart}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

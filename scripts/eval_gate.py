"""Eval gate (F15): exit non-zero when metrics breach eval/thresholds.json.

.venv/bin/python -m scripts.eval_gate                 # replay: score a recorded run, no LLM
.venv/bin/python -m scripts.eval_gate --mode live     # run the real agent (costs money)
Refresh the recording with: scripts.eval_run --record eval/fixtures/recorded_run.json
"""

import argparse
import asyncio

from app.config import get_settings
from app.eval.dataset import EvalDataset
from app.eval.gate import Thresholds, check_gate
from app.eval.pipeline import default_run_id, execute_run, score, write_outputs
from app.eval.report import pct
from app.eval.store import read_run_file


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--mode", choices=["replay", "live"], default="replay")
    p.add_argument("--thresholds", default="eval/thresholds.json")
    p.add_argument("--fixture", default="eval/fixtures/recorded_run.json")
    p.add_argument("--dataset", default="eval/datasets/eval-100.json")
    p.add_argument("--concurrency", type=int, default=4)
    a = p.parse_args()

    thresholds = Thresholds.load(a.thresholds)
    if a.mode == "replay":
        info, rows = read_run_file(a.fixture)
        scored = score(info, rows)
        print(
            f"Replaying recorded run {info.run_id} ({len(rows)} alerts), no LLM calls"
        )
    else:
        s = get_settings()
        ds = EvalDataset.load(a.dataset)
        run_id = default_run_id(ds.name, s.llm_plan_model, s.llm_assess_model)
        scored = asyncio.run(execute_run(s, ds, run_id, a.concurrency))
        write_outputs(scored)
    m = scored.metrics
    print(
        f"recall {pct(m.overall.recall)}  precision {pct(m.overall.precision)}  "
        f"grounding {pct(m.grounding_rate)}  errors {pct(m.error_rate)}"
    )
    result = check_gate(m, thresholds)
    if result.passed:
        print("GATE PASSED")
        return 0
    print("GATE FAILED")
    for f in result.failures:
        print(f"  - {f}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

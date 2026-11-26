"""F16 failure drills: break something mid-run and check the system recovers.

.venv/bin/python -m scripts.loadtest_drill                 # all three, one after another
.venv/bin/python -m scripts.loadtest_drill --only redis-down
Drills (each is one load run on the fake LLM with a fault injected):
  llm-errors   10% of LLM calls fail transiently; retries should hide it
  redis-down   Redis stopped for 20 s at 30% progress; everything should degrade, not fail
  kill-worker  the worker is SIGKILLed at 30% progress and restarted; messages return
               after the SQS visibility timeout (~5 min) and cases resume from checkpoints
Recorded facts are added to the run's result JSON under "drill". The worker is
restored to the default (openai) provider at the end. Not part of the repeatable sweep.
"""

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import psycopg

from app.config import get_settings
from app.db.copy import conninfo
from app.loadtest.collect import count_unfinished
from app.loadtest.seed import LoadSet, eval_alert_ids, reset
from app.messaging.sqs import open_queue
from scripts.loadtest_sweep import HIGH, Step, compose, start_workers

DRILLS = ("llm-errors", "redis-down", "kill-worker")


def docker(*args: str) -> str:
    return subprocess.run(
        ["docker", *args], check=False, capture_output=True, text=True
    ).stdout.strip()


async def _purge() -> None:
    settings = get_settings()
    if (
        settings.aws_endpoint_url is None
    ):  # local run: LocalStack and its dummy credentials
        settings = settings.model_copy(
            update={
                "aws_endpoint_url": "http://localhost:4566",
                "aws_access_key_id": settings.aws_access_key_id or "test",
                "aws_secret_access_key": settings.aws_secret_access_key or "test",
            }
        )
    async with open_queue(settings) as queue:
        await queue._sqs.purge_queue(QueueUrl=queue.queue_url)


def facts(url: str, alert_ids: list[int]) -> dict[str, Any]:
    with psycopg.connect(conninfo(url)) as conn:
        calls = conn.execute(
            "SELECT count(*), count(*) FILTER (WHERE l.error IS NOT NULL), "
            "count(*) FILTER (WHERE l.fallback) FROM llm_calls l "
            "JOIN cases c ON c.case_id = l.case_id WHERE c.alert_id = ANY(%s)",
            (alert_ids,),
        ).fetchone()
        cases = conn.execute(
            "SELECT count(*) FILTER (WHERE attempts > 1), "
            "count(*) FILTER (WHERE status = 'failed'), "
            "count(*) FILTER (WHERE status = 'running') "
            "FROM cases WHERE alert_id = ANY(%s)",
            (alert_ids,),
        ).fetchone()
    assert calls is not None and cases is not None
    return {
        "llm_calls": calls[0],
        "llm_call_errors": calls[1],
        "fallback_calls": calls[2],
        "cases_delivered_more_than_once": cases[0],
        "cases_failed": cases[1],
        "cases_still_running": cases[2],
    }


def wait_progress(
    url: str, ids: list[int], fraction: float, proc: subprocess.Popen[str]
) -> bool:
    """Block until `fraction` of the alerts are finished (or the runner exits)."""
    target = int(len(ids) * (1 - fraction))
    while proc.poll() is None:
        if count_unfinished(url, ids) <= target:
            return True
        time.sleep(2)
    return False


def run_drill(name: str, count: int, latency_ms: int, timeout: int) -> dict[str, Any]:
    settings = get_settings()
    url = settings.sync_database_url
    ids = LoadSet.load("loadtest/datasets/load-1000.json").alert_ids[:count]
    env = {**HIGH}
    if name == "llm-errors":
        env["FAKE_LLM_ERROR_RATE"] = "0.1"
    step = Step(f"drill-{name}", 8, count, env=env)
    start_workers(step, latency_ms)
    # Reset here, not in the runner: progress is measured from "no cases yet", and old
    # finished cases from an earlier drill would look like instant progress.
    reset(url, ids, eval_alert_ids("eval/datasets/eval-100.json"))
    asyncio.run(_purge())  # leftovers from an earlier or aborted run
    time.sleep(2)
    cmd = [
        sys.executable, "-m", "scripts.loadtest_run", "--count", str(count),
        "--label", f"drill-{name}", "--timeout", str(timeout),
        "--note", f"drill {name}", "--allow-dlq", "--no-reset",
    ]  # fmt: skip
    proc = subprocess.Popen(cmd, text=True)
    timeline: dict[str, Any] = {}
    if name != "llm-errors" and wait_progress(url, ids, 0.3, proc):
        timeline["fault_at_unfinished"] = count_unfinished(url, ids)
        assert timeline["fault_at_unfinished"] > 0, "fault must land mid-run"
        timeline["fault_epoch"] = time.time()
        if name == "redis-down":
            docker("stop", "aml-redis")
            time.sleep(20)
            docker("start", "aml-redis")
        else:
            timeline["running_cases_at_kill"] = facts(url, ids)["cases_still_running"]
            docker("kill", "aml-worker")
            time.sleep(5)
            docker("start", "aml-worker")
        timeline["recovered_epoch"] = time.time()
    proc.wait()
    timeline["runner_exit_code"] = proc.returncode
    timeline.update(facts(url, ids))
    return timeline


def annotate(results_dir: str, name: str, timeline: dict[str, Any]) -> None:
    files = sorted(Path(results_dir).glob("*.json"), key=os.path.getmtime)
    if not files:
        return
    f = files[-1]
    data = json.loads(f.read_text())
    data["drill"] = {"name": name, **timeline}
    f.write_text(json.dumps(data, indent=2) + "\n")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--only", nargs="*", choices=DRILLS)
    p.add_argument("--count", type=int, default=200)
    p.add_argument("--latency-ms", type=int, default=1500)
    p.add_argument("--timeout", type=int, default=1200)
    p.add_argument("--results-dir", default="loadtest/results")
    a = p.parse_args()
    try:
        for name in a.only or DRILLS:
            print(f"== drill {name}", flush=True)
            timeline = run_drill(name, a.count, a.latency_ms, a.timeout)
            annotate(a.results_dir, name, timeline)
            print(json.dumps(timeline, indent=2), flush=True)
    finally:
        compose("start", "redis")
        compose(
            "up", "-d", "--force-recreate", "worker", env={"LLM_PROVIDER": "openai"}
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

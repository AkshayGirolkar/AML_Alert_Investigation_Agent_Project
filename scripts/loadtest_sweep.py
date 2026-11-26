"""Run the F16 parameter sweep: restart the worker per configuration, then load-test it.

.venv/bin/python -m scripts.loadtest_sweep            # the default sweep
.venv/bin/python -m scripts.loadtest_sweep --only c8 c16
Each step recreates the worker container(s) with the given env, so it replaces whatever
your worker is doing. At the end it restores the default (openai) worker. The limiter is
raised for the sweep so worker concurrency, not the shared LLM rate limit, is what
varies; one extra step ("limiter") keeps the default 5 calls/s to record that ceiling.
"""

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from app.loadtest.report import sweep_table

HIGH = {"LLM_RATE_LIMIT_PER_SECOND": "1000", "LLM_RATE_LIMIT_BURST": "1000"}


@dataclass(frozen=True)
class Step:
    label: str
    concurrency: int
    count: int
    workers: int = 1
    env: dict[str, str] = field(default_factory=dict)


STEPS = [
    # first pass over alerts 1-400 is cold (table pages not cached): kept in the table
    # as its own row, and it warms the cache for every step after it
    Step("cold-c16", 16, 400, env=HIGH),
    Step("c1", 1, 60, env=HIGH),
    Step("c4", 4, 200, env=HIGH),
    Step("c8", 8, 200, env=HIGH),
    Step("c16", 16, 400, env=HIGH),
    Step("c32", 32, 400, env=HIGH),
    Step("c64", 64, 400, env=HIGH),
    Step("2w-c16", 16, 400, workers=2, env=HIGH),
    Step("c32-pool16", 32, 400, env={**HIGH, "WORKER_DB_POOL_SIZE": "16"}),
    Step("c64-pool16", 64, 400, env={**HIGH, "WORKER_DB_POOL_SIZE": "16"}),
    Step("limiter", 4, 100),  # default 5 LLM calls/s: the designed ceiling
    Step("full-c32", 32, 1000, env=HIGH),  # the 1,000-alert burst (600 of them cold)
]


def compose(*args: str, env: dict[str, str] | None = None) -> None:
    subprocess.run(
        ["docker", "compose", *args],
        check=True,
        env={**os.environ, **(env or {})},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def start_workers(step: Step, latency_ms: int) -> None:
    env = {
        "LLM_PROVIDER": "fake",
        "FAKE_LLM_LATENCY_MS": str(latency_ms),
        "WORKER_CONCURRENCY": str(step.concurrency),
        **step.env,
    }
    if step.workers == 2:
        compose(
            "--profile",
            "scale",
            "up",
            "-d",
            "--force-recreate",
            "worker",
            "worker2",
            env=env,
        )
    else:
        compose("--profile", "scale", "rm", "-sf", "worker2")
        compose("up", "-d", "--force-recreate", "worker", env=env)
    time.sleep(8)  # let the worker open its pools and start polling


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--only", nargs="*", help="step labels to run")
    p.add_argument("--latency-ms", type=int, default=1500)
    p.add_argument("--timeout", type=int, default=3000)
    p.add_argument("--results-dir", default="loadtest/results")
    a = p.parse_args()
    steps = [s for s in STEPS if not a.only or s.label in a.only]
    results = []
    try:
        for step in steps:
            print(
                f"== {step.label}: {step.workers} worker(s) x {step.concurrency}, "
                f"{step.count} alerts",
                flush=True,
            )
            start_workers(step, a.latency_ms)
            cmd = [
                sys.executable, "-m", "scripts.loadtest_run",
                "--count", str(step.count), "--profile", "burst",
                "--label", step.label, "--workers", str(step.workers),
                "--timeout", str(a.timeout), "--results-dir", a.results_dir,
                "--note", f"sweep {step.label}",
            ]  # fmt: skip
            proc = subprocess.run(cmd, check=False)
            files = sorted(Path(a.results_dir).glob("*.json"), key=os.path.getmtime)
            if files:
                results.append(json.loads(files[-1].read_text()))
            if proc.returncode not in (0, 1):
                print(f"step {step.label} could not run (exit {proc.returncode})")
                break
    finally:
        compose("--profile", "scale", "rm", "-sf", "worker2")
        compose(
            "up", "-d", "--force-recreate", "worker", env={"LLM_PROVIDER": "openai"}
        )
    table = sweep_table(results)
    out = Path(a.results_dir) / f"sweep-{time.strftime('%Y%m%dT%H%M%S')}.md"
    out.write_text(table)
    print(table)
    print(f"Wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

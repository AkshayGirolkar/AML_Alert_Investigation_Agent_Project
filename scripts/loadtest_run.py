"""Run one F16 load test: enqueue the load set through the API and measure the drain.

.venv/bin/python -m scripts.loadtest_run --count 50 --profile burst
.venv/bin/python -m scripts.loadtest_run --count 1000 --profile steady --rate 5

Needs the stack up with the worker on the fake LLM:
  LLM_PROVIDER=fake docker compose up -d --build
Writes loadtest/results/<run_id>.json and .md. Exits 1 if the run did not drain, has
duplicates, or left messages in the DLQ (unless --allow-dlq).
"""

import argparse
import asyncio
import json
import platform
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy.engine import make_url

from app.config import get_settings
from app.loadtest.collect import (
    count_unfinished,
    fetch_case_rows,
    total_cost,
    wait_for_drain,
)
from app.loadtest.enqueue import enqueue_all
from app.loadtest.metrics import compute_run_metrics
from app.loadtest.report import (
    build_result,
    enqueue_stats,
    is_clean,
    render_summary,
    worker_config,
)
from app.loadtest.sampler import (
    Sampler,
    docker_probe,
    postgres_probe,
    redis_probe,
    sqs_probe,
)
from app.loadtest.seed import LoadSet, eval_alert_ids, is_local, reset
from app.messaging.sqs import open_queue
from app.redis_client import make_redis

CONTAINERS = ["aml-worker", "aml-api", "aml-postgres", "aml-redis"]


def _git_sha() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except OSError:
        return "unknown"


def _worker_env(container: str) -> list[str] | None:
    try:
        out = subprocess.run(
            ["docker", "inspect", container, "--format", "{{json .Config.Env}}"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        env = json.loads(out)
        return env if isinstance(env, list) else None
    except (OSError, subprocess.CalledProcessError, json.JSONDecodeError):
        return None


async def run(a: argparse.Namespace) -> int:
    settings = get_settings()
    if settings.aws_endpoint_url is None and not a.force:
        # local run: LocalStack with its dummy credentials, like docker compose uses
        settings = settings.model_copy(
            update={
                "aws_endpoint_url": "http://localhost:4566",
                "aws_access_key_id": settings.aws_access_key_id or "test",
                "aws_secret_access_key": settings.aws_secret_access_key or "test",
            }
        )
    url = a.database_url or settings.sync_database_url
    target = make_url(url)
    print(f"Target database: {target.database} on {target.host}; API: {a.api_url}")
    api_local = httpx.URL(a.api_url).host in ("localhost", "127.0.0.1")
    if not (is_local(url) and api_local) and not a.force:
        print("Refusing: database or API is not local (use --force).")
        return 2

    manifest = Path(f"loadtest/datasets/{a.name}.json")
    if not manifest.exists():
        print(f"No manifest {manifest}; run scripts.loadtest_seed first.")
        return 2
    alert_ids = LoadSet.load(manifest).alert_ids[: a.count]

    env = _worker_env(a.worker_container)
    cfg = worker_config(env) if env else {}
    fake = cfg.get("LLM_PROVIDER") == "fake"
    if not fake and not a.live:
        print(
            "The worker is not on the fake LLM (LLM_PROVIDER=fake); real OpenAI calls "
            "cost money. Restart it with LLM_PROVIDER=fake, or pass --live "
            "--max-cost-usd N."
        )
        return 2
    if a.live and not a.max_cost_usd:
        print("--live needs --max-cost-usd.")
        return 2

    if not a.no_reset:
        counts = reset(url, alert_ids, eval_alert_ids(a.eval_dataset))
        print("Reset:", {k: v for k, v in counts.items() if v})

    run_id = f"{datetime.now(UTC):%Y%m%dT%H%M%S}-{a.profile}-{len(alert_ids)}"
    redis = make_redis(settings)
    async with open_queue(settings) as queue:
        if (await queue.depths())[0] > 0 and not a.force:
            print("Queue is not empty; drain or purge it first (or --force).")
            return 2
        probes = {
            "postgres": postgres_probe(url),
            "sqs": sqs_probe(queue.depths),
            "redis": redis_probe(redis),
            "docker": docker_probe(CONTAINERS),
        }
        sampler = Sampler(probes, a.sample_interval)
        sampler.start()

        async def unfinished() -> int:
            return await asyncio.to_thread(count_unfinished, url, alert_ids)

        async def over_budget() -> bool:
            if not a.live:
                return False
            return await asyncio.to_thread(total_cost, url, alert_ids) > a.max_cost_usd

        print(f"Enqueueing {len(alert_ids)} alerts ({a.profile})...")
        async with httpx.AsyncClient(base_url=a.api_url, timeout=30) as client:
            sent = await enqueue_all(
                client, alert_ids, a.profile, a.rate, a.enqueue_concurrency
            )
        print(
            f"Enqueued in {sent.ended - sent.started:.1f}s "
            f"({sent.errors} errors); waiting for the queue to drain..."
        )
        outcome = await wait_for_drain(unfinished, a.timeout, abort=over_budget)
        samples = await sampler.stop()
        _, dlq = await queue.depths()
        if outcome == "aborted":
            await queue._sqs.purge_queue(QueueUrl=queue.queue_url)
            print(f"Cost cap ${a.max_cost_usd} exceeded: queue purged.")
    await redis.aclose()

    rows = await asyncio.to_thread(fetch_case_rows, url, alert_ids)
    metrics = compute_run_metrics(rows, dlq=dlq, trim=a.trim)
    params: dict[str, Any] = {
        "run": {
            "profile": a.profile,
            "count": len(alert_ids),
            "rate": a.rate if a.profile == "steady" else None,
            "enqueue_concurrency": a.enqueue_concurrency,
            "manifest": str(manifest),
            "trim": a.trim,
            "hardware": f"{platform.machine()} {platform.system()}",
            "note": a.note,
        },
        "worker": cfg,
    }
    result = build_result(
        run_id,
        _git_sha(),
        a.profile,
        params,
        metrics,
        enqueue_stats(sent),
        outcome,
        samples,
    )
    out = Path(a.results_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{run_id}.json").write_text(json.dumps(result, indent=2) + "\n")
    summary = render_summary(result)
    (out / f"{run_id}.md").write_text(summary)
    print(summary)
    clean = is_clean(result, a.allow_dlq)
    print("RESULT:", "clean" if clean else "NOT CLEAN")
    return 0 if clean else 1


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--name", default="load-1000", help="manifest name")
    p.add_argument("--count", type=int, default=1000)
    p.add_argument("--profile", choices=["burst", "steady"], default="burst")
    p.add_argument("--rate", type=float, default=5.0, help="steady: requests/second")
    p.add_argument("--enqueue-concurrency", type=int, default=50)
    p.add_argument("--api-url", default="http://localhost:8000")
    p.add_argument("--worker-container", default="aml-worker")
    p.add_argument(
        "--timeout", type=float, default=1800, help="seconds to wait to drain"
    )
    p.add_argument("--sample-interval", type=float, default=5.0)
    p.add_argument("--trim", type=float, default=0.1)
    p.add_argument("--no-reset", action="store_true")
    p.add_argument("--allow-dlq", action="store_true", help="a drill may dead-letter")
    p.add_argument("--live", action="store_true", help="allow real OpenAI calls")
    p.add_argument("--max-cost-usd", type=float, default=None)
    p.add_argument("--note", default="", help="free text recorded with the run")
    p.add_argument("--force", action="store_true")
    p.add_argument("--eval-dataset", default="eval/datasets/eval-100.json")
    p.add_argument("--results-dir", default="loadtest/results")
    p.add_argument("--database-url", default=None)
    return asyncio.run(run(p.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())

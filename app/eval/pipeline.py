"""Glue for the eval scripts: build the agent, run a set, persist, score, report (F15)."""

import asyncio
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from langgraph.checkpoint.memory import MemorySaver

from app.agent.gateway import MemoryRecorder, build_gateway
from app.agent.graph import build_graph
from app.agent.tools.registry import make_conn_factory
from app.config import Settings
from app.eval.baseline import Comparison, baseline_metrics, compare
from app.eval.dataset import EvalDataset
from app.eval.metrics import EvalMetrics, ResultRow, compute_metrics
from app.eval.report import render_chart, render_report
from app.eval.runner import run_eval
from app.eval.store import (
    RunInfo,
    finish_run,
    load_results,
    save_result,
    save_run,
    write_run_file,
)
from app.redis_client import make_redis
from app.services.rate_limiter import RedisTokenBucket
from app.synth.embeddings import OpenAIEmbedder


def git_sha() -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def default_run_id(dataset: str, planner: str, assess: str) -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    return f"{dataset}-{planner}-{assess}-{stamp}".replace("/", "_")


async def _limiter(settings: Settings) -> RedisTokenBucket | None:
    """The shared F10 limiter if Redis is reachable; otherwise None (still runs)."""
    redis = make_redis(settings)
    try:
        await asyncio.wait_for(redis.ping(), timeout=3)
    except (OSError, TimeoutError, Exception):  # noqa: BLE001 - any Redis trouble degrades
        await redis.aclose()
        return None
    return RedisTokenBucket(
        redis,
        "ratelimit:llm",
        settings.llm_rate_limit_per_second,
        settings.llm_rate_limit_burst,
        settings.llm_rate_limit_max_wait_seconds,
    )


@dataclass
class Scored:
    info: RunInfo
    rows: list[ResultRow]
    metrics: EvalMetrics
    comparison: Comparison


def score(info: RunInfo, rows: list[ResultRow]) -> Scored:
    m = compute_metrics(rows)
    return Scored(info, rows, m, compare(m, baseline_metrics(rows)))


def write_outputs(s: Scored, out_dir: str | Path = "eval") -> tuple[Path, Path, Path]:
    base = Path(out_dir)
    run_file = base / "runs" / f"{s.info.run_id}.json"
    report = base / "reports" / f"{s.info.run_id}.md"
    chart = base / "reports" / f"{s.info.run_id}.png"
    write_run_file(run_file, s.info, s.rows)
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(render_report(s.info, s.metrics, s.comparison, s.rows))
    render_chart(s.metrics, s.comparison, chart)
    return run_file, report, chart


async def execute_run(
    settings: Settings,
    dataset: EvalDataset,
    run_id: str,
    concurrency: int = 4,
    resume: bool = False,
    persist: bool = True,
) -> Scored:
    """Run the real agent over the set. Needs OPENAI_API_KEY and the dataset's alerts."""
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY is not set (see .env.example)")
    url = settings.sync_database_url
    info = RunInfo(
        run_id=run_id,
        dataset=dataset.name,
        planner_model=settings.llm_plan_model,
        assess_model=settings.llm_assess_model,
        git_sha=git_sha(),
        config={
            "concurrency": concurrency,
            "seed": dataset.seed,
            "size": len(dataset.items),
        },
    )
    recorder = MemoryRecorder()
    limiter = await _limiter(settings)
    graph = build_graph(
        make_conn_factory(url),
        plan_llm=build_gateway(
            settings,
            "plan",
            settings.llm_plan_model,
            settings.llm_fallback_plan_model,
            recorder,
            limiter,
        ),
        assess_llm=build_gateway(
            settings,
            "assess",
            settings.llm_assess_model,
            settings.llm_fallback_assess_model,
            recorder,
            limiter,
        ),
        embedder=OpenAIEmbedder(settings.openai_api_key),
        settings=settings,
        checkpointer=MemorySaver(),  # needed for the human_review interrupt; never resumed
        store=None,  # no long-term memory: runs must not influence each other
    )
    done: dict[int, ResultRow] = {}
    if persist:
        await asyncio.to_thread(save_run, url, info)
        if resume:
            done = {
                r.alert_id: r
                for r in await asyncio.to_thread(load_results, url, run_id)
            }

    async def on_result(row: ResultRow) -> None:
        if persist:
            await asyncio.to_thread(save_result, url, run_id, row)

    rows = await run_eval(
        dataset, graph, recorder, run_id, concurrency, done, on_result
    )
    if persist:
        await asyncio.to_thread(finish_run, url, run_id)
    return score(info, rows)

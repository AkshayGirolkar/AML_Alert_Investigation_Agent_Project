"""Result file and Markdown summary of one load run (F16). Pure: no I/O."""

from dataclasses import asdict
from typing import Any

from app.loadtest.enqueue import EnqueueResult
from app.loadtest.metrics import ApiStats, LoadMetrics, Percentiles, api_stats

# Worker settings that decide the result; a run without them is not comparable.
CONFIG_KEYS = (
    "LLM_PROVIDER",
    "FAKE_LLM_LATENCY_MS",
    "FAKE_LLM_JITTER_MS",
    "FAKE_LLM_ERROR_RATE",
    "FAKE_LLM_ESCALATE_PCT",
    "WORKER_CONCURRENCY",
    "WORKER_DB_POOL_SIZE",
    "LLM_RATE_LIMIT_PER_SECOND",
    "LLM_RATE_LIMIT_BURST",
    "SQS_VISIBILITY_TIMEOUT",
)


def worker_config(env: list[str]) -> dict[str, str]:
    """Pick CONFIG_KEYS out of a container's `KEY=value` environment list."""
    found = dict(e.split("=", 1) for e in env if "=" in e)
    return {k: found[k] for k in CONFIG_KEYS if k in found}


def enqueue_stats(e: EnqueueResult) -> ApiStats:
    return api_stats(e.latencies_ms, e.errors, e.total)


def build_result(
    run_id: str,
    git_sha: str,
    profile: str,
    params: dict[str, Any],
    metrics: LoadMetrics,
    api: ApiStats,
    outcome: str,
    samples: list[dict[str, Any]],
) -> dict[str, Any]:
    f = metrics.failures
    return {
        "run_id": run_id,
        "git_sha": git_sha,
        "profile": profile,
        "outcome": outcome,  # drained | timeout | aborted
        "params": params,
        "cases": metrics.n_cases,
        "finished": metrics.n_finished,
        "elapsed_seconds": metrics.elapsed_seconds,
        "throughput_per_min": metrics.throughput_per_min,
        "steady_throughput_per_min": metrics.steady_throughput_per_min,
        "latency": {
            "e2e": asdict(metrics.e2e),
            "queue_wait": asdict(metrics.queue_wait),
            "run_time": asdict(metrics.run_time),
        },
        "api": {
            "latency_ms": asdict(api.latency_ms),
            "requests": api.requests,
            "errors": api.errors,
            "error_rate": api.error_rate,
        },
        "failures": asdict(f),
        "cost_usd": metrics.cost_usd,
        "tokens": {"input": metrics.input_tokens, "output": metrics.output_tokens},
        "samples": samples,
    }


def is_clean(result: dict[str, Any], allow_dlq: bool = False) -> bool:
    """The hard pass/fail of a run: drained, nothing duplicated, nothing dead-lettered."""
    f = result["failures"]
    return (
        result["outcome"] == "drained"
        and f["duplicates"] == 0
        and f["unfinished"] == 0
        and (allow_dlq or f["dlq"] == 0)
    )


def _num(v: float | None, digits: int = 1) -> str:
    return "n/a" if v is None else f"{v:.{digits}f}"


def _row(name: str, p: dict[str, Any] | Percentiles, unit: str) -> str:
    d = p if isinstance(p, dict) else asdict(p)
    return (
        f"| {name} | {_num(d['p50'])} | {_num(d['p95'])} | {_num(d['p99'])} | "
        f"{d['n']} | {unit} |"
    )


def render_summary(result: dict[str, Any]) -> str:
    f = result["failures"]
    lat = result["latency"]
    cfg = result["params"].get("worker", {})
    lines = [
        f"# Load run {result['run_id']}",
        "",
        (
            f"- Profile: **{result['profile']}**, outcome: **{result['outcome']}**, "
            f"git `{result['git_sha']}`"
        ),
        (
            f"- Cases: {result['finished']}/{result['cases']} finished in "
            f"{_num(result['elapsed_seconds'])} s"
        ),
        (
            f"- Throughput: **{_num(result['throughput_per_min'])}** cases/min "
            f"overall, **{_num(result['steady_throughput_per_min'])}** steady state"
        ),
        (
            f"- Correctness: errors {f['errors']}, unfinished {f['unfinished']}, "
            f"redeliveries {f['redeliveries']}, duplicates **{f['duplicates']}**, "
            f"DLQ **{f['dlq']}**"
        ),
        (
            f"- LLM: cost ${result['cost_usd']:.4f}, tokens "
            f"{result['tokens']['input']} in / {result['tokens']['output']} out"
        ),
        "",
        "| Latency | p50 | p95 | p99 | n | unit |",
        "|---|---|---|---|---|---|",
        _row("end to end", lat["e2e"], "s"),
        _row("queue wait", lat["queue_wait"], "s"),
        _row("run time", lat["run_time"], "s"),
        _row("enqueue API", result["api"]["latency_ms"], "ms"),
        "",
        (
            f"Enqueue API: {result['api']['requests']} requests, "
            f"{result['api']['errors']} errors."
        ),
        "",
        "## Config",
        "",
        *[
            f"- `{k}` = {v}"
            for k, v in {**result["params"].get("run", {}), **cfg}.items()
        ],
        "",
    ]
    return "\n".join(lines)


def sweep_table(results: list[dict[str, Any]]) -> str:
    """One row per run: the table the ADR and README quote."""
    head = (
        "| run | workers x conc | alerts | cases/min (steady) | e2e p95 s | run p95 s "
        "| api p95 ms | errors | dup | dlq | outcome |\n"
        "|---|---|---|---|---|---|---|---|---|---|---|"
    )
    rows = []
    for r in results:
        w = r["params"].get("worker", {})
        run = r["params"].get("run", {})
        f = r["failures"]
        label = run.get("label") or r["run_id"]
        shape = f"{run.get('workers', 1)} x {w.get('WORKER_CONCURRENCY', '?')}"
        rows.append(
            f"| {label} | {shape} | {r['cases']} "
            f"| {_num(r['steady_throughput_per_min'])} "
            f"| {_num(r['latency']['e2e']['p95'])} "
            f"| {_num(r['latency']['run_time']['p95'])} "
            f"| {_num(r['api']['latency_ms']['p95'], 0)} "
            f"| {f['errors']} | {f['duplicates']} | {f['dlq']} | {r['outcome']} |"
        )
    return "\n".join([head, *rows]) + "\n"

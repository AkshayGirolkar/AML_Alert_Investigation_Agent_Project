"""Load-test metrics (F16). Pure functions: no DB, queue or LLM, so they are unit-tested.

Input is one `CaseRow` per case, built by the runner from `cases` and `llm_calls` (times
as epoch seconds). Notes on what the data can and cannot say:
- `started_at` is the worker's first LLM call minus its latency, because `cases` has no
  start time. It is a close proxy (load_alert/load_kyc run just before it), so queue wait
  is slightly understated and run time slightly overstated.
- An escalated case stops at `awaiting_review`; for load tests that counts as finished
  (`finished_at` is `cases.updated_at`, since `completed_at` stays empty for it).
- All arithmetic here and in SQL, never by an LLM.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from app.eval.metrics import percentile

FINISHED = frozenset({"completed", "awaiting_review", "failed"})
FAILED = "failed"


@dataclass(frozen=True)
class CaseRow:
    alert_id: int
    status: str
    enqueued_at: float  # cases.created_at
    started_at: float | None = None  # first LLM call start (proxy for worker start)
    finished_at: float | None = None  # cases.updated_at once the status is final
    attempts: int = 1  # cases.attempts; > 1 means the message was delivered again
    plan_calls_ok: int = 1  # successful `plan` LLM calls; > 1 means it ran twice
    decision_count: int = 0  # decisions rows (must stay 0 or 1)
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def finished(self) -> bool:
        return self.status in FINISHED and self.finished_at is not None


@dataclass(frozen=True)
class Percentiles:
    p50: float | None
    p95: float | None
    p99: float | None
    n: int


def percentiles(values: Sequence[float]) -> Percentiles:
    return Percentiles(
        percentile(values, 50),
        percentile(values, 95),
        percentile(values, 99),
        len(values),
    )


@dataclass(frozen=True)
class Failures:
    errors: int  # cases that ended `failed`
    unfinished: int  # cases still queued/running when the run was measured
    redeliveries: int  # cases delivered more than once
    duplicates: int  # cases with duplicated side effects (must be 0)
    dlq: int  # messages in the dead-letter queue


@dataclass(frozen=True)
class LoadMetrics:
    n_cases: int
    n_finished: int
    elapsed_seconds: float | None  # first enqueue -> last finish
    throughput_per_min: float | None  # n_finished over elapsed_seconds
    steady_throughput_per_min: float | None  # middle of the run only
    steady_window: tuple[float, float] | None
    e2e: Percentiles  # enqueue -> finish
    queue_wait: Percentiles  # enqueue -> worker start
    run_time: Percentiles  # worker start -> finish
    failures: Failures
    cost_usd: float
    input_tokens: int
    output_tokens: int


def is_duplicate(row: CaseRow) -> bool:
    """A case whose investigation ran twice, or that has more than one decision."""
    return row.plan_calls_ok > 1 or row.decision_count > 1


def steady_state(
    finish_times: Sequence[float], trim: float = 0.1
) -> tuple[float, int, float] | None:
    """(window start, finishes strictly after it up to the end, window end).

    Drops the first and last `trim` share of finishes, which are ramp-up and the drain
    tail. None when fewer than 3 finishes remain, because a rate needs an interval.
    """
    if not 0 <= trim < 0.5:
        raise ValueError("trim must be in [0, 0.5)")
    ordered = sorted(finish_times)
    cut = int(len(ordered) * trim)
    kept = ordered[cut : len(ordered) - cut]
    if len(kept) < 3 or kept[-1] <= kept[0]:
        return None
    return kept[0], len(kept) - 1, kept[-1]


def compute_run_metrics(
    rows: Sequence[CaseRow], dlq: int = 0, trim: float = 0.1
) -> LoadMetrics:
    done = [r for r in rows if r.finished]
    ok = [r for r in done if r.status != FAILED]
    elapsed: float | None = None
    rate: float | None = None
    if done:
        last = max(r.finished_at for r in done if r.finished_at is not None)
        first = min(r.enqueued_at for r in rows)
        elapsed = last - first
        rate = len(done) / elapsed * 60 if elapsed > 0 else None

    steady = steady_state(
        [r.finished_at for r in done if r.finished_at is not None], trim
    )
    steady_rate = window = None
    if steady is not None:
        start, count, end = steady
        steady_rate = count / (end - start) * 60
        window = (start, end)

    e2e = [r.finished_at - r.enqueued_at for r in ok if r.finished_at is not None]
    queue = [r.started_at - r.enqueued_at for r in ok if r.started_at is not None]
    run = [
        r.finished_at - r.started_at
        for r in ok
        if r.started_at is not None and r.finished_at is not None
    ]
    return LoadMetrics(
        n_cases=len(rows),
        n_finished=len(done),
        elapsed_seconds=elapsed,
        throughput_per_min=rate,
        steady_throughput_per_min=steady_rate,
        steady_window=window,
        e2e=percentiles(e2e),
        queue_wait=percentiles(queue),
        run_time=percentiles(run),
        failures=Failures(
            errors=sum(r.status == FAILED for r in rows),
            unfinished=sum(not r.finished for r in rows),
            redeliveries=sum(r.attempts > 1 for r in rows),
            duplicates=sum(is_duplicate(r) for r in rows),
            dlq=dlq,
        ),
        cost_usd=sum(r.cost_usd for r in rows),
        input_tokens=sum(r.input_tokens for r in rows),
        output_tokens=sum(r.output_tokens for r in rows),
    )


@dataclass(frozen=True)
class ApiStats:
    latency_ms: Percentiles
    requests: int
    errors: int
    error_rate: float | None


def api_stats(latencies_ms: Sequence[float], errors: int, total: int) -> ApiStats:
    """Enqueue-endpoint latency and error rate; `latencies_ms` are successful calls."""
    return ApiStats(
        percentiles(latencies_ms),
        total,
        errors,
        errors / total if total else None,
    )

"""F16 unit tests: load-test metric math. No DB, queue or LLM."""

import pytest

from app.loadtest.metrics import (
    CaseRow,
    api_stats,
    compute_run_metrics,
    is_duplicate,
    percentiles,
    steady_state,
)


def row(i: int, enq: float, start: float, fin: float, **kw: object) -> CaseRow:
    kw.setdefault("status", "completed")
    return CaseRow(i, enqueued_at=enq, started_at=start, finished_at=fin, **kw)  # type: ignore[arg-type]


def test_percentiles_single_value_and_empty() -> None:
    one = percentiles([4.0])
    assert (one.p50, one.p95, one.p99, one.n) == (4.0, 4.0, 4.0, 1)
    empty = percentiles([])
    assert empty.p50 is None and empty.n == 0


def test_empty_run() -> None:
    m = compute_run_metrics([])
    assert m.n_cases == 0 and m.throughput_per_min is None
    assert m.e2e.p50 is None and m.failures.errors == 0


def test_latency_split_and_throughput() -> None:
    # 4 cases enqueued at t=0; each waits 10s, runs 20s; finishes staggered by 10s
    rows = [row(i, 0, 10 + i * 10, 30 + i * 10) for i in range(4)]
    m = compute_run_metrics(rows)
    assert m.n_finished == 4 and m.elapsed_seconds == 60
    assert m.throughput_per_min == pytest.approx(4.0)
    assert m.queue_wait.p50 == pytest.approx(25.0)
    assert m.run_time.p50 == pytest.approx(20.0)
    assert m.e2e.p50 == pytest.approx(45.0)


def test_steady_state_excludes_ramp_and_tail() -> None:
    # 10 finishes at t=1..10 but with a slow first and a long tail
    times = [50.0, 51, 52, 53, 54, 55, 56, 57, 58, 200.0]
    s = steady_state(times, trim=0.1)
    assert s is not None
    start, count, end = s
    assert (start, end) == (51, 58) and count == 7  # 1 finish dropped on each side
    rows = [row(i, 0, 1, t) for i, t in enumerate(times)]
    m = compute_run_metrics(rows, trim=0.1)
    assert m.steady_window == (51, 58)
    assert m.steady_throughput_per_min == pytest.approx(7 / 7 * 60)
    assert m.steady_throughput_per_min > m.throughput_per_min  # type: ignore[operator]


def test_steady_state_too_few_finishes() -> None:
    assert steady_state([1.0, 2.0]) is None
    assert steady_state([5.0, 5.0, 5.0, 5.0]) is None  # zero-length window
    with pytest.raises(ValueError):
        steady_state([1.0], trim=0.5)


def test_failures_redeliveries_unfinished_and_dlq() -> None:
    rows = [
        row(1, 0, 1, 10),
        row(2, 0, 1, 12, attempts=2),
        row(3, 0, 1, 13, status="failed"),
        CaseRow(4, status="running", enqueued_at=0),
        row(5, 0, 1, 14, status="awaiting_review"),
    ]
    m = compute_run_metrics(rows, dlq=2)
    f = m.failures
    assert (f.errors, f.unfinished, f.redeliveries, f.dlq, f.duplicates) == (
        1,
        1,
        1,
        2,
        0,
    )
    assert m.n_finished == 4  # awaiting_review and failed are final
    assert m.e2e.n == 3  # the failed case is not a latency sample


def test_duplicate_detection() -> None:
    assert not is_duplicate(row(1, 0, 1, 2))
    assert is_duplicate(row(1, 0, 1, 2, plan_calls_ok=2))
    assert is_duplicate(row(1, 0, 1, 2, decision_count=2))
    assert not is_duplicate(row(1, 0, 1, 2, decision_count=1))
    m = compute_run_metrics([row(1, 0, 1, 2, plan_calls_ok=2), row(2, 0, 1, 3)])
    assert m.failures.duplicates == 1


def test_cost_and_tokens_summed() -> None:
    rows = [
        row(1, 0, 1, 2, cost_usd=0.5, input_tokens=100, output_tokens=10),
        row(2, 0, 1, 3, cost_usd=0.25, input_tokens=50, output_tokens=5),
    ]
    m = compute_run_metrics(rows)
    assert (m.cost_usd, m.input_tokens, m.output_tokens) == (0.75, 150, 15)


def test_unfinished_only_has_no_rate() -> None:
    m = compute_run_metrics([CaseRow(1, status="queued", enqueued_at=0)])
    assert m.n_finished == 0 and m.throughput_per_min is None
    assert m.failures.unfinished == 1


def test_api_stats() -> None:
    s = api_stats([10, 20, 30], errors=1, total=4)
    assert s.error_rate == 0.25 and s.latency_ms.p50 == 20
    assert api_stats([], 0, 0).error_rate is None

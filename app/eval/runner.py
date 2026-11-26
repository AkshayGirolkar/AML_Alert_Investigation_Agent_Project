"""Run the investigation graph over an eval set and score each verdict (F15).

The graph input is only `{"alert_id": ...}`: ground truth never reaches the agent. Escalated
alerts stop at `human_review` (a checkpointer is required) and are never resumed, so the
verdict is scored before any analyst. Failures are rows, not exceptions: an alert that
crashed counts as a miss. LLM cost comes from the gateway's in-process recorder, keyed
by case_id (= alert_id via `case_scope`), because eval has no `cases` rows.
"""

import asyncio
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import replace
from typing import Any, Protocol

from app.agent.gateway import CallRecord, MemoryRecorder, case_scope
from app.eval.dataset import Candidate, EvalDataset
from app.eval.metrics import ResultRow


class Graph(Protocol):
    async def ainvoke(self, input: Any, config: Any = None) -> dict[str, Any]: ...


OnResult = Callable[[ResultRow], Awaitable[None] | None]


def row_from_state(
    item: Candidate,
    state: Mapping[str, Any],
    records: Iterable[CallRecord],
    seconds: float,
) -> ResultRow:
    verdict = state.get("verdict") or {}
    node_costs: dict[str, float] = defaultdict(float)
    for r in records:
        node_costs[r.node] += r.cost_usd
    return ResultRow(
        alert_id=item.alert_id,
        rule_id=item.rule_id,
        truth=item.truth,
        truth_typology=item.truth_typology,
        outcome=state.get("outcome"),
        typology=verdict.get("typology"),
        risk=verdict.get("risk_score"),
        confidence=verdict.get("confidence"),
        grounded=bool(state.get("grounded")),
        n_citations=len(verdict.get("evidence_ids") or []),
        tool_calls=int(state.get("tool_calls") or 0),
        cost_usd=sum(node_costs.values()),
        seconds=round(seconds, 2),
        budget_exceeded=bool(state.get("budget_exceeded")),
        rationale=str(verdict.get("rationale") or ""),
        node_costs=dict(node_costs),
    )


async def _run_one(
    graph: Graph,
    recorder: MemoryRecorder,
    run_id: str,
    item: Candidate,
) -> ResultRow:
    start = time.monotonic()
    state: dict[str, Any] = {}
    error: str | None = None
    try:
        with case_scope(item.alert_id):
            state = await graph.ainvoke(
                {"alert_id": item.alert_id},
                {"configurable": {"thread_id": f"eval-{run_id}-{item.alert_id}"}},
            )
    except Exception as exc:  # noqa: BLE001 - one bad alert must not stop the run
        error = f"{type(exc).__name__}: {exc}"[:300]
    row = row_from_state(
        item,
        state,
        [r for r in recorder.records if r.case_id == item.alert_id],
        time.monotonic() - start,
    )
    if error is not None:
        row = _with_error(row, error)
    return row


def _with_error(row: ResultRow, error: str) -> ResultRow:
    return replace(row, error=error, outcome=None)


async def run_eval(
    dataset: EvalDataset,
    graph: Graph,
    recorder: MemoryRecorder,
    run_id: str,
    concurrency: int = 4,
    done: Mapping[int, ResultRow] | None = None,
    on_result: OnResult | None = None,
) -> list[ResultRow]:
    """Score every alert in the set; `done` rows (a resumed run) are not re-run.

    `on_result` is called as each alert finishes, so results can be persisted as they come.
    """
    done = dict(done or {})
    todo = [i for i in dataset.items if i.alert_id not in done]
    sem = asyncio.Semaphore(max(1, concurrency))

    async def work(item: Candidate) -> ResultRow:
        async with sem:
            row = await _run_one(graph, recorder, run_id, item)
        if on_result is not None:
            res = on_result(row)
            if isinstance(res, Awaitable):
                await res
        return row

    fresh = await asyncio.gather(*(work(i) for i in todo))
    rows = [*done.values(), *fresh]
    return sorted(rows, key=lambda r: r.alert_id)

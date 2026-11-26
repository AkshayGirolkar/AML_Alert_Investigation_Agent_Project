import asyncio
from typing import Any

from app.agent.gateway import CallRecord, MemoryRecorder, case_scope
from app.eval.dataset import Candidate, EvalDataset
from app.eval.runner import run_eval

ITEMS = [
    Candidate(1, "fan_in", True, "CYCLE"),
    Candidate(2, "fan_in", False, None),
    Candidate(3, "fan_out", True, "STACK"),
    Candidate(4, "fan_out", False, None),
]
DATASET = EvalDataset("t", 1, 4, {}, ITEMS)


def rec(case_id: int, node: str, cost: float) -> CallRecord:
    return CallRecord(case_id, node, 1, "openai", "m", 10, 5, cost, 100, 1, False, None)


class FakeGraph:
    """Escalates odd alerts, closes even ones, crashes on alert 3 when asked."""

    def __init__(self, recorder: MemoryRecorder, crash_on: int | None = None) -> None:
        self.recorder = recorder
        self.crash_on = crash_on
        self.inputs: list[Any] = []
        self.configs: list[Any] = []
        self.active = 0
        self.peak = 0

    async def ainvoke(self, input: Any, config: Any = None) -> dict[str, Any]:
        self.inputs.append(input)
        self.configs.append(config)
        self.active += 1
        self.peak = max(self.peak, self.active)
        await asyncio.sleep(0.01)
        self.active -= 1
        aid = input["alert_id"]
        if aid == self.crash_on:
            raise RuntimeError("tool exploded")
        with case_scope(aid):
            await self.recorder.record(rec(aid, "plan", 0.01))
            await self.recorder.record(rec(aid, "assess", 0.03))
        escalate = aid % 2 == 1
        return {
            "outcome": "escalated" if escalate else "closed",
            "verdict": {
                "typology": "cycle" if escalate else None,
                "risk_score": 0.9 if escalate else 0.1,
                "confidence": 0.8,
                "evidence_ids": ["ev-1", "ev-2"],
                "rationale": "because",
            },
            "grounded": True,
            "tool_calls": 3,
        }


async def test_scores_rows_with_cost_and_never_leaks_truth() -> None:
    recorder = MemoryRecorder()
    graph = FakeGraph(recorder)
    rows = await run_eval(DATASET, graph, recorder, "r1")
    assert [r.alert_id for r in rows] == [1, 2, 3, 4]
    r1 = rows[0]
    assert r1.escalated and r1.typology == "cycle" and r1.n_citations == 2
    assert r1.cost_usd == 0.04 and r1.node_costs == {"plan": 0.01, "assess": 0.03}
    assert rows[1].closed and r1.truth and not rows[1].truth
    # leakage guard: the graph sees only the alert id, plus a thread id
    assert all(i == {"alert_id": i["alert_id"]} for i in graph.inputs)
    assert all(set(c["configurable"]) == {"thread_id"} for c in graph.configs)


async def test_errors_are_rows_and_count_as_misses() -> None:
    recorder = MemoryRecorder()
    rows = await run_eval(DATASET, FakeGraph(recorder, crash_on=3), recorder, "r1")
    bad = rows[2]
    assert bad.error and "tool exploded" in bad.error and bad.outcome is None
    assert not bad.escalated and bad.truth  # a missed true alert
    assert len(rows) == 4  # the run kept going


async def test_concurrency_is_bounded() -> None:
    recorder = MemoryRecorder()
    graph = FakeGraph(recorder)
    await run_eval(DATASET, graph, recorder, "r1", concurrency=2)
    assert graph.peak == 2


async def test_resume_skips_done_and_streams_results() -> None:
    recorder = MemoryRecorder()
    first = await run_eval(DATASET, FakeGraph(recorder), recorder, "r1")
    done = {r.alert_id: r for r in first if r.alert_id in (1, 2)}

    graph = FakeGraph(MemoryRecorder())
    seen: list[int] = []
    rows = await run_eval(
        DATASET,
        graph,
        graph.recorder,
        "r1",
        done=done,
        on_result=lambda r: seen.append(r.alert_id),
    )
    assert sorted(i["alert_id"] for i in graph.inputs) == [3, 4]
    assert sorted(seen) == [3, 4]
    assert [r.alert_id for r in rows] == [1, 2, 3, 4]
    assert rows[0] is done[1]

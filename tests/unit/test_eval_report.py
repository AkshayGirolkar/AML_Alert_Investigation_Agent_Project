from pathlib import Path
from typing import Any

from app.eval.baseline import baseline_metrics, compare
from app.eval.dataset import Candidate, EvalDataset
from app.eval.langsmith_sync import sync_run
from app.eval.metrics import ResultRow, compute_metrics
from app.eval.report import render_chart, render_report
from app.eval.store import RunInfo

INFO = RunInfo("r1", "ds", "gpt-4o-mini", "gpt-4o", "abc", {})


def rows() -> list[ResultRow]:
    def mk(i: int, truth: bool, outcome: str | None, typ: str | None = None, **kw: Any):
        return ResultRow(
            i, "fan_in", truth, typ, outcome=outcome,
            typology="money mule" if outcome == "escalated" else None,
            grounded=True, n_citations=1, cost_usd=0.02, seconds=3.0,
            node_costs={"plan": 0.005, "assess": 0.015}, rationale="why", **kw,
        )  # fmt: skip

    return [
        mk(1, True, "escalated", "CYCLE"),
        mk(2, True, "closed", "STACK"),
        mk(3, False, "closed"),
        mk(4, False, "escalated"),
    ]


def test_report_contains_the_headline_numbers() -> None:
    r = rows()
    m = compute_metrics(r)
    text = render_report(INFO, m, compare(m, baseline_metrics(r)), r)
    assert "Agent vs rules-only baseline" in text
    assert "| Recall | 100.0% | 50.0%" in text
    assert "| CYCLE | 1 |" in text and "| STACK | 1 |" in text
    assert "`money mule` x2" in text  # unmapped typology is surfaced
    assert "alert 2" in text  # the missed true alert is listed
    assert "$0.0200" in text


def test_report_handles_empty_metrics() -> None:
    m = compute_metrics([])
    text = render_report(INFO, m, compare(m, baseline_metrics([])), [])
    assert "n/a" in text


def test_chart_is_written(tmp_path: Path) -> None:
    r = rows()
    m = compute_metrics(r)
    out = tmp_path / "x" / "chart.png"
    render_chart(m, compare(m, baseline_metrics(r)), out)
    assert out.stat().st_size > 1000


class FakeClient:
    def __init__(self, exists: bool = False, fail: bool = False) -> None:
        self.exists, self.fail = exists, fail
        self.examples: list[Any] = []
        self.runs: list[dict[str, Any]] = []

    def has_dataset(self, dataset_name: str) -> bool:
        return self.exists

    def create_dataset(self, name: str, description: str = "") -> Any:
        return type("D", (), {"id": "ds-1"})()

    def create_examples(self, dataset_id: str, examples: list[Any]) -> None:
        self.examples += examples

    def create_run(self, **kw: Any) -> None:
        if self.fail:
            raise RuntimeError("network")
        self.runs.append(kw)


def test_langsmith_sync_uploads_masked_data_and_never_raises() -> None:
    r = rows()
    m = compute_metrics(r)
    ds = EvalDataset(
        "ds",
        1,
        4,
        {},
        [Candidate(i.alert_id, i.rule_id, i.truth, i.truth_typology) for i in r],
    )
    c = FakeClient()
    assert sync_run(c, ds, INFO, r, m) is True
    assert len(c.examples) == 4 and len(c.runs) == 4
    assert c.examples[0]["inputs"] == {"alert_id": 1, "rule_id": "fan_in"}
    assert c.runs[0]["project_name"] == "aml-eval-r1"

    again = FakeClient(exists=True)
    sync_run(again, ds, INFO, r, m)
    assert again.examples == []  # dataset is uploaded once

    assert sync_run(FakeClient(fail=True), ds, INFO, r, m) is False


def test_report_has_named_columns_and_confusion_table() -> None:
    from app.eval.metrics import ResultRow

    rows = [
        ResultRow(
            1, "fan_out", True, "FAN-OUT", outcome="escalated", typology="fan out"
        ),
        ResultRow(2, "fan_out", True, "STACK", outcome="escalated", typology=None),
    ]
    m = compute_metrics(rows)
    text = render_report(INFO, m, compare(m, baseline_metrics(rows)), rows)
    assert "Right when named" in text and "## Typology confusion" in text
    assert "| FAN-OUT | FAN-OUT x1 |" in text and "| STACK | (none) x1 |" in text
    table = next(ln for ln in text.splitlines() if ln.startswith("| **All**"))
    assert table.count("|") == 8  # matches the 7-column header

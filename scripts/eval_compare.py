"""Compare two eval runs side by side (F15).

.venv/bin/python -m scripts.eval_compare RUN_A RUN_B
A run is an id in the database or a path to a run JSON file.
"""

import argparse
from pathlib import Path

from app.config import get_settings
from app.eval.metrics import EvalMetrics, ResultRow, compute_metrics
from app.eval.report import pct
from app.eval.store import RunInfo, load_info, load_results, read_run_file


def load(ref: str, url: str) -> tuple[RunInfo, list[ResultRow]]:
    if Path(ref).is_file():
        return read_run_file(ref)
    info = load_info(url, ref)
    if info is None:
        raise SystemExit(f"run not found: {ref}")
    return info, load_results(url, ref)


def usd(x: float | None) -> str:
    return "n/a" if x is None else f"${x:.4f}"


def table(a: tuple[RunInfo, EvalMetrics], b: tuple[RunInfo, EvalMetrics]) -> str:
    (ia, ma), (ib, mb) = a, b
    lines = [
        f"{'':<26}{ia.run_id[:30]:<32}{ib.run_id[:30]:<32}",
        (
            f"{'planner / assess':<26}{ia.planner_model + ' / ' + ia.assess_model:<32}"
            f"{ib.planner_model + ' / ' + ib.assess_model:<32}"
        ),
    ]
    rows = [
        ("recall", pct(ma.overall.recall), pct(mb.overall.recall)),
        (
            "false closure rate",
            pct(ma.overall.false_closure_rate),
            pct(mb.overall.false_closure_rate),
        ),
        ("precision", pct(ma.overall.precision), pct(mb.overall.precision)),
        (
            "typology accuracy",
            pct(ma.overall.typology_accuracy),
            pct(mb.overall.typology_accuracy),
        ),
        ("grounding rate", pct(ma.grounding_rate), pct(mb.grounding_rate)),
        ("error rate", pct(ma.error_rate), pct(mb.error_rate)),
        ("mean cost / case", usd(ma.cost.mean), usd(mb.cost.mean)),
        ("p95 cost / case", usd(ma.cost.p95), usd(mb.cost.p95)),
        (
            "mean seconds / case",
            f"{ma.seconds.mean or 0:.1f}",
            f"{mb.seconds.mean or 0:.1f}",
        ),
        (
            "p95 seconds / case",
            f"{ma.seconds.p95 or 0:.1f}",
            f"{mb.seconds.p95 or 0:.1f}",
        ),
    ]
    lines += [f"{name:<26}{x:<32}{y:<32}" for name, x, y in rows]
    typologies = sorted(set(ma.by_typology) | set(mb.by_typology))
    if typologies:
        lines.append("\nrecall by typology")
        for t in typologies:
            ga, gb = ma.by_typology.get(t), mb.by_typology.get(t)
            lines.append(
                f"  {t:<24}{pct(ga.recall if ga else None):<32}{pct(gb.recall if gb else None):<32}"
            )
    return "\n".join(lines)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("run_a")
    p.add_argument("run_b")
    a = p.parse_args()
    url = get_settings().sync_database_url
    ia, ra = load(a.run_a, url)
    ib, rb = load(a.run_b, url)
    print(table((ia, compute_metrics(ra)), (ib, compute_metrics(rb))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

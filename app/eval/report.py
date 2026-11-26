"""Markdown report and agent-vs-baseline chart for an eval run (F15)."""

from collections.abc import Sequence
from pathlib import Path

from app.eval.baseline import Comparison
from app.eval.metrics import EvalMetrics, GroupMetrics, ResultRow
from app.eval.store import RunInfo


def pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x * 100:.1f}%"


def _ci(x: tuple[float, float] | None) -> str:
    return "" if x is None else f" ({x[0] * 100:.0f}-{x[1] * 100:.0f}%)"


def _usd(x: float | None) -> str:
    return "n/a" if x is None else f"${x:.4f}"


def _secs(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.1f}s"


def _group_line(name: str, g: GroupMetrics) -> str:
    return (
        f"| {name} | {g.true_alerts} | {pct(g.recall)}{_ci(g.recall_ci)} | "
        f"{pct(g.false_closure_rate)} | {pct(g.typology_accuracy)} |"
    )


def render_report(
    info: RunInfo,
    m: EvalMetrics,
    cmp: Comparison,
    rows: Sequence[ResultRow],
    worst: int = 10,
) -> str:
    o = m.overall
    out = [
        f"# Evaluation report: {info.run_id}",
        "",
        (
            f"Dataset `{info.dataset}`, {o.n} alerts ({o.true_alerts} true, "
            f"{o.false_alerts} false). Planner `{info.planner_model}`, assess "
            f"`{info.assess_model}`, git `{info.git_sha or 'unknown'}`."
        ),
        "",
        "## Agent vs rules-only baseline",
        "",
        (
            "Rules-only means an analyst works every alert (escalate all), so its recall on "
            "alerted launderers is 100% by construction. The question is how much workload "
            "the agent removes and how many launderers it lets through."
        ),
        "",
        "| | Rules only | Agent |",
        "|---|---|---|",
        (
            f"| Recall | {pct(cmp.baseline.recall)} | "
            f"{pct(cmp.agent.recall)}{_ci(cmp.agent.recall_ci)} |"
        ),
        (
            f"| Precision of escalations | {pct(cmp.baseline.precision)} | "
            f"{pct(cmp.agent.precision)}{_ci(cmp.agent.precision_ci)} |"
        ),
        f"| Alerts sent to an analyst | {cmp.baseline.escalated} | {cmp.agent.escalated} |",
        f"| False closures (true alerts missed) | 0 | {cmp.true_alerts_missed} |",
        f"| False alerts closed | 0 | {cmp.false_alerts_closed} |",
        "",
        (
            f"Workload reduction {pct(cmp.workload_reduction)}, recall lost "
            f"{pct(cmp.recall_lost)}."
        ),
        "",
        "## Recall by typology",
        "",
        "| Typology | True alerts | Recall (95% CI) | False closure | Typology accuracy | Named | Right when named |",
        "|---|---|---|---|---|---|---|",
        _group_line("**All**", o),
        *(_group_line(t, g) for t, g in m.by_typology.items()),
        "",
        (
            "Typology accuracy counts escalated true alerts whose named typology matches "
            "the IBM patterns file; a null typology counts as wrong. 'Named' is the share "
            "where the agent named a pattern at all."
        ),
        "",
        "## Quality, cost and latency",
        "",
        (
            f"- Grounding rate: {pct(m.grounding_rate)} of scored verdicts are grounded and "
            "cite at least one evidence id"
        ),
        f"- Error rate: {pct(m.error_rate)}",
        f"- Mean tool calls: {m.mean_tool_calls if m.mean_tool_calls is None else f'{m.mean_tool_calls:.1f}'}",
        (
            f"- Cost per case: mean {_usd(m.cost.mean)}, p50 {_usd(m.cost.p50)}, "
            f"p95 {_usd(m.cost.p95)}"
        ),
        (
            f"- Latency per case: mean {_secs(m.seconds.mean)}, p50 {_secs(m.seconds.p50)}, "
            f"p95 {_secs(m.seconds.p95)}"
        ),
    ]
    node_totals: dict[str, float] = {}
    for r in rows:
        for node, cost in r.node_costs.items():
            node_totals[node] = node_totals.get(node, 0.0) + cost
    if node_totals and rows:
        out.append(
            "- Mean cost by node: "
            + ", ".join(
                f"{n} {_usd(c / len(rows))}" for n, c in sorted(node_totals.items())
            )
        )
    if m.unmapped_typologies:
        out += [
            "",
            "## Typology names not recognised",
            "",
            (
                "These count as wrong; extend the alias map in `app/eval/typology.py` if "
                "they are valid names."
            ),
            "",
            *(f"- `{n}` x{c}" for n, c in m.unmapped_typologies.items()),
        ]
    misses = [r for r in rows if r.truth and not r.escalated]
    if misses:
        out += ["", f"## Missed true alerts (first {worst})", ""]
        for r in misses[:worst]:
            why = r.error or ("over budget" if r.budget_exceeded else r.rationale)
            out.append(
                f"- alert {r.alert_id} ({r.rule_id}, {r.truth_typology or 'untyped'}), "
                f"risk {r.risk}, confidence {r.confidence}: {why[:200]}"
            )
    return "\n".join(out) + "\n"


def render_chart(m: EvalMetrics, cmp: Comparison, path: str | Path) -> None:
    """Left: agent vs baseline. Right: agent recall by typology with 95% intervals."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, (ax1, ax2) = plt.subplots(
        1, 2, figsize=(11, 4.2), gridspec_kw={"width_ratios": [1, 1.6]}
    )
    labels = ["Recall", "Precision"]
    base = [cmp.baseline.recall or 0.0, cmp.baseline.precision or 0.0]
    agent = [cmp.agent.recall or 0.0, cmp.agent.precision or 0.0]
    x = range(len(labels))
    ax1.bar([i - 0.2 for i in x], base, 0.4, label="Rules only", color="#9aa5b1")
    ax1.bar([i + 0.2 for i in x], agent, 0.4, label="Agent", color="#2b6cb0")
    ax1.set_xticks(list(x), labels)
    ax1.set_ylim(0, 1.05)
    ax1.set_title("Agent vs rules-only baseline")
    ax1.legend(frameon=False)

    names = list(m.by_typology)
    vals = [m.by_typology[t].recall or 0.0 for t in names]
    errs = [[0.0] * len(names), [0.0] * len(names)]
    for i, t in enumerate(names):
        ci = m.by_typology[t].recall_ci
        if ci and m.by_typology[t].recall is not None:
            errs[0][i] = max(0.0, vals[i] - ci[0])
            errs[1][i] = max(0.0, ci[1] - vals[i])
    ax2.bar(names, vals, yerr=errs, color="#2b6cb0", capsize=3)
    ax2.axhline(1.0, color="#9aa5b1", linestyle="--", linewidth=1, label="Rules only")
    ax2.set_ylim(0, 1.05)
    ax2.set_title("Agent recall by typology (95% CI)")
    ax2.tick_params(axis="x", labelrotation=35)
    for side in ("top", "right"):
        ax1.spines[side].set_visible(False)
        ax2.spines[side].set_visible(False)
    fig.tight_layout()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=150)
    plt.close(fig)

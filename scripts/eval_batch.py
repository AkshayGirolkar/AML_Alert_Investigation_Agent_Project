"""Quick batch check of the F06 agent against ground truth (NOT the F15 harness).

Samples a small, deterministic, stratified set of alerts (per rule: some true and some
false positives), runs the agent on each, and compares verdicts with `is_true_positive`.
The labels are read here only for scoring; the agent never sees them.

.venv/bin/python -m scripts.eval_batch --per-class 3 --workers 4 --out results.json
"""

import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import psycopg

from app.agent.graph import build_graph
from app.agent.llm import OpenAILLM
from app.agent.tools.registry import make_conn_factory
from app.config import get_settings
from app.db.copy import conninfo
from app.synth.embeddings import OpenAIEmbedder

# Deterministic order: hash of the id, so reruns pick the same alerts.
SAMPLE_SQL = """
SELECT alert_id, rule_id, is_true_positive, typology FROM (
    SELECT alert_id, rule_id, is_true_positive, typology,
           row_number() OVER (
               PARTITION BY rule_id, is_true_positive ORDER BY md5(alert_id::text)
           ) AS rn
    FROM alerts
) t WHERE rn <= %s ORDER BY rule_id, is_true_positive DESC, rn
"""


def sample_alerts(database_url: str, per_class: int) -> list[dict[str, Any]]:
    with psycopg.connect(conninfo(database_url)) as conn:
        rows = conn.execute(SAMPLE_SQL, (per_class,)).fetchall()
    return [
        {"alert_id": r[0], "rule_id": r[1], "truth": bool(r[2]), "truth_typology": r[3]}
        for r in rows
    ]


def run_one(graph: Any, item: dict[str, Any]) -> dict[str, Any]:
    start = time.monotonic()
    out: dict[str, Any] = {**item}
    try:
        state = graph.invoke({"alert_id": item["alert_id"]})
        v = state["verdict"]
        out.update(
            outcome=state["outcome"],
            grounded=state["grounded"],
            retries=state["grounding_retries"],
            tool_calls=state["tool_calls"],
            risk=v["risk_score"],
            confidence=v["confidence"],
            typology=v["typology"],
            rationale=v["rationale"],
        )
    except Exception as exc:  # noqa: BLE001 - keep the batch going, report at the end
        out["error"] = f"{type(exc).__name__}: {exc}"[:300]
    out["seconds"] = round(time.monotonic() - start, 1)
    return out


def summarize(results: list[dict[str, Any]]) -> str:
    ok = [r for r in results if "error" not in r]
    lines = [f"Alerts run: {len(results)}  errors: {len(results) - len(ok)}"]
    tp = [r for r in ok if r["truth"]]
    fp = [r for r in ok if not r["truth"]]
    caught = sum(r["outcome"] == "escalated" for r in tp)
    false_closed = len(tp) - caught
    escalated_fp = sum(r["outcome"] == "escalated" for r in fp)
    lines.append(
        f"Recall (true alerts escalated): {caught}/{len(tp)}"
        f"   false closures: {false_closed}"
    )
    lines.append(
        f"False alarms escalated: {escalated_fp}/{len(fp)}"
        f"   correctly closed: {len(fp) - escalated_fp}/{len(fp)}"
    )
    esc = [r for r in ok if r["outcome"] == "escalated"]
    if esc:
        lines.append(f"Precision of escalations: {caught}/{len(esc)}")
    named = [r for r in tp if r["outcome"] == "escalated" and r["truth_typology"]]
    if named:
        hits = sum(
            (r["typology"] or "").upper() == r["truth_typology"].upper() for r in named
        )
        lines.append(f"Typology matches (escalated true alerts): {hits}/{len(named)}")
    lines.append(
        f"Ungrounded after retry: {sum(not r['grounded'] for r in ok)}   "
        f"needed a retry: {sum(r['retries'] > 0 for r in ok)}   "
        f"avg tool calls: {sum(r['tool_calls'] for r in ok) / max(len(ok), 1):.1f}   "
        f"avg seconds: {sum(r['seconds'] for r in ok) / max(len(ok), 1):.1f}"
    )
    lines.append("\nPer rule (true: escalated/total | false: closed/total)")
    for rule in sorted({r["rule_id"] for r in ok}):
        t = [r for r in tp if r["rule_id"] == rule]
        f = [r for r in fp if r["rule_id"] == rule]
        lines.append(
            f"  {rule:<24} true {sum(r['outcome'] == 'escalated' for r in t)}/{len(t)}"
            f" | false {sum(r['outcome'] == 'closed' for r in f)}/{len(f)}"
        )
    lines.append("\nWrong decisions")
    for r in ok:
        if r["truth"] and r["outcome"] == "closed":
            kind = "FALSE CLOSURE"
        elif not r["truth"] and r["outcome"] == "escalated":
            kind = "false escalation"
        else:
            continue
        lines.append(
            f"  {kind:<16} alert {r['alert_id']} {r['rule_id']} "
            f"risk={r['risk']:.2f} conf={r['confidence']:.2f}"
        )
    for r in results:
        if "error" in r:
            lines.append(f"  ERROR alert {r['alert_id']}: {r['error']}")
    return "\n".join(lines)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--per-class", type=int, default=3, help="true + false alerts per rule"
    )
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--out", default=None, help="write per-alert results as JSON")
    a = p.parse_args()

    s = get_settings()
    if not s.openai_api_key:
        p.error("OPENAI_API_KEY is not set")
    graph = build_graph(
        make_conn_factory(s.sync_database_url),
        plan_llm=OpenAILLM(s.llm_plan_model, s.openai_api_key),
        assess_llm=OpenAILLM(s.llm_assess_model, s.openai_api_key),
        embedder=OpenAIEmbedder(s.openai_api_key),
        settings=s,
    )
    items = sample_alerts(s.sync_database_url, a.per_class)
    print(f"Running {len(items)} alerts with {a.workers} workers...")
    with ThreadPoolExecutor(max_workers=a.workers) as pool:
        results = list(pool.map(lambda it: run_one(graph, it), items))
    if a.out:
        with open(a.out, "w") as f:
            json.dump(results, f, indent=2, default=str)
    print(summarize(results))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

# ADR 0008 – F15 evaluation harness

## Status
Accepted

## Decisions
- **Score alerts, before the analyst.** The runner calls the graph with only `{"alert_id": ...}` and never resumes `human_review`; an escalated alert counts as caught. Ground truth (`alerts.is_true_positive`, `alerts.typology`) is read only by the scorer; a test pins the graph input.
- **Frozen dataset, committed.** `eval/datasets/eval-100.json`: 50 true alerts spread over the 8 IBM typologies plus untyped, 50 false alerts spread over the rules, ordered by `md5(seed:alert_id)`. Same database and seed give the same bytes. It is built from the full HI-Small alerts; the 100k dev slice has too few true alerts per typology.
- **Untyped true alerts are their own group.** 1,259 of 2,231 true alerts have no laundering-attempt typology (trigger transactions are laundering but outside a listed attempt). They count in recall but not in typology accuracy.
- **Errors are misses.** A crashed, over-budget or failed alert is a row with an error and counts as an unescalated true alert, never dropped.
- **Cost without `cases` rows.** `llm_calls.case_id` has a foreign key to `cases`, and eval must not write case tables, so cost comes from the gateway's in-process `MemoryRecorder` keyed by alert id (`case_scope`) and is stored in `eval_results`. No long-term memory store, so runs cannot influence each other.
- **Baseline = escalate every alert.** The F04 rules hand analysts the whole queue, so recall on alerted launderers is 100% by construction. The comparison is precision, analyst workload and recall lost.
- **Typology: free text plus alias map.** The agent prompt is unchanged. Scoring normalises names to the IBM vocabulary; a null or unknown name counts as wrong. The report also shows how often the agent named a pattern at all and how often it was right when it did, because a bare accuracy hides the difference.
- **Gate: replay by default.** `scripts.eval_gate --mode replay` scores `eval/fixtures/recorded_run.json` with no LLM; `--mode live` runs the agent. Thresholds (`eval/thresholds.json`) are floors set below the first real run, not tuned to it. Per-typology groups have 5-6 alerts, so one miss moves a group by 17-20 points; the typology floor is deliberately loose (0.5).
- **Wilson intervals** on recall and precision, because groups are small.

## First real run (`full-100-v1`, gpt-4o-mini planner, gpt-4o assess)
Recall 90.0% (95% CI 79-96%), false closures 5, precision 55.6% against 50.0% for rules-only, 81 of 100 alerts sent to an analyst (19% workload reduction), grounding 100%, errors 0%. Mean cost $0.0175 per case (p95 $0.0323), mean 11.5 s. Weakest typology: GATHER-SCATTER (66.7%, n=6). **Typology accuracy is 0%**: the agent names a typology on 31 of 82 escalations and rarely on true alerts (its prompt says to use null unless the structure is visible); when it did name one for a true alert it was wrong (CYCLE for BIPARTITE and SCATTER-GATHER). The 100 alerts also happen to be the ones used to set the thresholds.

## Consequences
- The eval set is used to read the results; any prompt or threshold tuning needs a separate held-out set first.
- Typology naming is the obvious next improvement (constrain the agent to the IBM list, or ask for it in the verdict schema) and would be scored as a separate run.
- No GitHub Actions workflow yet: CI does not exist. It can call the replay gate on PRs and the live gate on `main`.
- Two-model comparison is supported (`--planner-model`, `--assess-model`, `scripts.eval_compare`) but only one model pair has been run.
- `LANGSMITH` sync exists behind `--langsmith` and is tested with a fake client only.

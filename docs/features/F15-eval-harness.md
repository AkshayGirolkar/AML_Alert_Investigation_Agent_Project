# F15 – Evaluation Harness

## Goal
Measure how well the investigation agent finds real laundering, per typology, and compare it with the rules-only baseline from F04. The output is the headline table and chart for the README, plus a CI gate that fails a PR when recall drops.

## In scope
- **Frozen eval set** (`app/eval/dataset.py`): a deterministic, stratified sample of alerts (target 100 for the CI gate, a larger set for full runs), picked by `md5(alert_id)` order within each `(rule_id, is_true_positive, typology)` stratum, like `scripts/eval_batch.py` does now. Written to `eval/datasets/<name>.json` (alert ids + ground truth + seed + strata counts) and committed, so every run and every PR scores the same alerts. Ground truth comes from `alerts.is_true_positive` / `alerts.typology` (F04). The agent never sees it.
- **Runner** (`app/eval/runner.py`): runs the F06/F12 graph directly on each alert (no SQS, no human review, in-memory checkpointer, memory store off so runs don't contaminate each other). Concurrency is bounded and goes through the F10 rate limiter and F12 gateway. A failed alert is recorded as an `error` row and counts as a miss, not dropped. Results land in `eval_runs` / `eval_results` (migration 0009: run id, git sha, dataset name, model config, per-alert verdict, outcome, grounded flag, tool calls, seconds, cost from `llm_calls`) and a `results.json` file.
- **Metrics** (`app/eval/metrics.py`, pure functions, no DB or LLM):
  - Recall on true alerts: escalated true alerts / true alerts. Reported overall and per typology.
  - False closure rate: closed true alerts / true alerts (= 1 − recall, shown separately because it is the costly error).
  - Escalation precision: escalated true alerts / escalated alerts. Also false-positive closure rate (share of false alerts correctly closed).
  - Typology accuracy: among escalated true alerts, agent typology equals the attempt typology after normalising names to the IBM patterns vocabulary (alias map in `app/eval/typology.py`; unknown names count as wrong, `None` for a true alert is wrong).
  - Grounding rate: share of verdicts that are grounded (all cited evidence ids exist in state, from `state["grounded"]`) and cite at least one id; plus the share of SAR-draft narrative citations that resolve. Claim-level checking beyond ids is out of scope.
  - Cost and latency per case: mean, p50, p95, from `llm_calls` and wall-clock seconds, total and per node.
  - Wilson 95% intervals on recall and precision, since per-typology groups are small.
- **Baseline comparison** (`app/eval/baseline.py`): rules-only = "escalate every alert" on the same alert set (what the F04 rules would send to an analyst), giving recall 1.0 by construction on rule-covered true alerts and precision equal to the alert true-positive rate. The comparison therefore centres on precision, analyst workload (escalations avoided) and recall lost; also report the F04 per-rule numbers on the same set for context. State this framing in the README so the chart is not misread.
- **Model comparison**: runner takes `--planner-model`, `--assess-model` (via the F12 settings overrides) and records them on the run, so GPT-4o vs a smaller planner is two runs on the same dataset. `scripts/eval_compare.py` prints a side-by-side table of two run ids.
- **Report** (`app/eval/report.py`): Markdown report (per-typology table, baseline comparison, cost/latency, worst misses with rationale) written to `eval/reports/<run_id>.md`, and a PNG/SVG chart of agent vs baseline. These feed F18.
- **LangSmith** (`app/eval/langsmith_sync.py`): upload the dataset once (examples keyed by alert id, inputs masked via the F13 masker, ground truth in reference outputs) and run each eval as an experiment with the metrics as evaluators, so prompt/model changes are comparable in the UI. Optional: skipped without a key; local results are the source of truth.
- **CI eval gate**: `scripts/eval_gate.py` runs the 100-alert set, compares with `eval/thresholds.json` (min recall overall, min recall per typology with n ≥ 5, min grounding rate, max mean cost per case, max error rate), and exits non-zero on breach. GitHub Actions workflow is added only if CI exists by then (see Open questions); otherwise the script and a Makefile-style command ship now.
- **CLIs**: `python -m scripts.eval_build_dataset`, `python -m scripts.eval_run`, `python -m scripts.eval_compare`, `python -m scripts.eval_gate`, each printing the target database as in F02–F04.
- Replace `scripts/eval_batch.py` (it says it is not the F15 harness); keep it until the new runner passes, then delete.
- ADR `0008-f15-evaluation-harness.md`; `docs/features/F15-explained.txt`; `.env.example` and CLAUDE.md updated.

## Out of scope
Load testing (F16), prompt tuning or threshold tuning against the eval set (a separate change, and only with a held-out split), the human-review step (verdicts are scored before the analyst), claim-level hallucination judging with an LLM, a dashboard in the console, AWS/Terraform.

## Interfaces
- `build_dataset(database_url, size, seed) -> EvalDataset` and `EvalDataset.save/load`.
- `run_eval(dataset, graph_factory, concurrency) -> EvalRun`; `graph_factory` is injectable so tests use a fake LLM.
- `compute_metrics(results) -> EvalMetrics` (overall + per typology, with intervals).
- `baseline_metrics(dataset) -> EvalMetrics`.
- `check_gate(metrics, thresholds) -> GateResult(passed, failures)`.
- Result row: `{alert_id, rule_id, truth, truth_typology, outcome, typology, risk, confidence, grounded, tool_calls, cost_usd, seconds, error}`.

## Rules to keep (from the design)
- **No label leakage:** ground truth is read only by the scorer. A test asserts that nothing from `is_true_positive`, `is_laundering`, or the patterns tables reaches graph input, prompts or tool output.
- **Honest set:** the dataset is frozen once and thresholds are never set by tuning prompts against it. If tuning is needed later, split into dev and held-out sets first.
- **Arithmetic in code:** all rates, intervals and percentiles are computed in `metrics.py`, never by an LLM.
- **Idempotent and resumable:** a run can be re-started with `--resume <run_id>` and skips alerts already scored; DB writes are keyed by `(run_id, alert_id)`.
- Eval runs write to a dedicated database or `eval_*` tables only; they never write `cases`, `decisions` or `audit_log` rows into the console-visible schema.

## Acceptance criteria
- Dataset build is deterministic: same DB and seed give an identical file; strata counts add up; every sampled alert has a label.
- Metric math is unit-tested without DB or LLM, including: zero true alerts in a typology, zero escalations (precision undefined → reported as n/a), all-error runs, typology alias normalisation, Wilson interval edge cases (0/n and n/n), percentile on one value.
- Runner with a fake graph/LLM: errors are recorded and counted as misses, concurrency is bounded, `--resume` skips finished alerts, cost and seconds are captured.
- Baseline comparison on a hand-built result set gives the expected recall, precision and escalations avoided.
- `check_gate` passes on healthy metrics and fails with a named reason for each breached threshold.
- Leakage test passes (see rules).
- Integration (marker `live`, real OpenAI, skipped by default): a 10-alert run on the dev slice completes, writes `eval_results`, and produces the report and chart.
- `scripts.eval_compare` shows two runs with different models side by side.
- ruff, mypy and pytest pass.

## Decisions (open questions resolved)
1. **Dataset source:** the eval set is built from the full HI-Small alerts (the dev slice has too few true alerts per typology). The manifest is committed, so a run only needs a database that contains those alerts and their transactions: the gate runs against a loaded full-data database (local, or a snapshot restored in CI later), never the 100k slice. Strata with fewer than 5 true alerts are reported but excluded from per-typology gate thresholds.
2. **CI gate:** two modes in `scripts/eval_gate.py`. `--mode replay` (default, used on PRs) scores recorded verdicts from `eval/fixtures/recorded_run.json` and tests the harness and thresholds without any LLM call. `--mode live` runs the real agent and is for `main`, nightly and manual runs, and the person refreshing the recording. Recording is `scripts.eval_run --record`. No GitHub Actions workflow in F15 (CI does not exist yet, and there is no `.github/`); the workflow comes with the CI feature and just calls these commands. Revisit live-on-PR once cost per run is known from the first real run.
3. **Baseline:** one baseline, "escalate every alert", plus the F04 per-rule numbers on the same set. No top-N-rules variant.
4. **Typology vocabulary:** the IBM names are BIPARTITE, CYCLE, FAN-IN, FAN-OUT, GATHER-SCATTER, RANDOM, SCATTER-GATHER, STACK. The agent stays free text (F06 prompt and schema untouched); scoring normalises with an alias map (case, hyphen/underscore/space, "fan in" etc.). Unmapped names count as wrong and are listed in the report so the alias map can grow. Constraining the agent to this list is a possible follow-up, scored separately.

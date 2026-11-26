# F19 – Typology naming in the agent verdict

## Goal
Raise typology accuracy (escalated true alerts whose named pattern matches the IBM patterns file) from 0% without hurting recall, and without overfitting the 100-alert eval set.

## Why it is 0% today (from `full-100-v1`)
- The prompt says to set `typology` only when the structure is clearly visible, and the field is free text. 45 true alerts were escalated; the agent named a pattern for 4 of them and got 0 right (BIPARTITE/FAN-IN/SCATTER-GATHER called CYCLE, FAN-OUT called FAN-IN). It also named a pattern for 4 false alerts, which the metric ignores.
- The rule that fired does not reveal the typology: in eval-100, `high_risk_counterparty` triggers 4-6 true alerts of every typology. The agent has to infer it from tool output.
- The IBM typology describes a whole laundering attempt across several accounts, while an alert is one account's local view. Some patterns (RANDOM, and likely STACK, BIPARTITE) may not be distinguishable from a 2-hop view at all. A prompt can't fix that, so the achievable ceiling is unknown until measured.

## In scope
1. **Held-out split first.** Build `eval/datasets/tune-100.json` with the F15 builder (different seed, excluding every alert in `eval-100`). Prompt iteration scores only against `tune-100`. `eval-100` is run once at the end as confirmation. (Its aggregate results and a few miss rationales have already been seen, so it is a confirmation set, not a pristine test set; the README says so.)
2. **Feasibility check (free, no LLM).** For the true alerts in `tune-100`, compute from the database the structural signals the tools expose (root degree in/out, 2-hop shape, cycles, counterparty counts, timing) and see which typologies separate. Output: a small table in the ADR of "distinguishable / not distinguishable from the local view". Typologies that can't be told apart are reported as such, not chased.
3. **Prompt and schema change.**
   - `Verdict.typology` becomes a closed set: the 8 IBM names or null (a `Literal`, so the scorer's alias map is no longer the safety net).
   - The system prompt gets a one-line structural definition of each pattern in terms of tool output (what FAN-OUT, CYCLE, SCATTER-GATHER, ... look like in `query_transactions` and `counterparty_network`), and says to name the closest matching pattern for an escalation and null for a closure or when none fits.
   - No mention of the rule name as a hint, and no label leakage (the agent never sees `typology` or `is_true_positive`; keep that test).
4. **Scoring additions (small).** Report, next to accuracy, the named rate and accuracy-when-named (already in `GroupMetrics`), plus a confusion table (true typology × named typology) in the report, because the confusions say where the evidence is insufficient.
5. **Docs:** ADR `0012-f19-typology-prompt.md` with before/after, `F19-explained.txt`, README headline/limitations and `docs/results/` refreshed together (the docs test enforces this), CLAUDE.md note.

## Out of scope
New tools, graph changes, model changes, threshold changes, changing close/escalate logic. If the feasibility check shows a needed signal is missing from the tools, that is recorded as a follow-up feature, not built here.

## Interfaces
`Verdict.typology: Literal[...] | None`; prompt text in `app/agent/prompts.py`; `eval/datasets/tune-100.json`; confusion table in `app/eval/report.py`. Persisted verdicts with other strings (old cases, fixtures) must still load: keep the stored shape tolerant on read.

## Rules to keep
- Never tune against `eval-100`; at most one confirmation run on it.
- Recall must not drop: acceptance compares against `full-100-v1` (90%) and the F15 gate thresholds.
- Report the result whatever it is; a modest or unchanged number is a finding, with the feasibility table as the explanation.
- Don't read `truth_typology` anywhere on the agent path.

## Acceptance criteria
- `tune-100` exists, is disjoint from `eval-100` (unit test), and is reproducible from its seed.
- Feasibility table committed in the ADR.
- Unit tests: closed-set schema rejects unknown names and accepts null; the prompt contains each of the 8 definitions; no label field reaches the agent.
- On `tune-100`, typology accuracy among caught true alerts beats the old prompt run on the same set, and recall is not lower by more than the interval noise (report both with Wilson intervals).
- One confirmation run on `eval-100`: recall >= 80% (the gate) and typology accuracy reported with the confusion table. Target, not a gate: >= 25% among caught alerts, to be revised after the feasibility check.
- README and `docs/results/` updated together; `tests/unit/test_docs.py` passes.

## Decisions (proposed, confirm before building)
1. **Spend.** Each 100-alert real run is about $2. Plan: 1 baseline run of the old prompt on `tune-100`, up to 3 prompt iterations, 1 confirmation on `eval-100`: about $10 total, hard cap $15. OK?
2. **Closed enum vs free text.** Recommended: closed enum plus null. Alternative: keep free text and rely on the alias map.
3. **Scope if the ceiling is low.** If the feasibility check shows only 2-3 typologies are separable, ship the prompt for those and say so, rather than adding tools. Recommended.

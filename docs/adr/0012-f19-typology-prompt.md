# ADR 0012 – F19 typology naming

## Status
Accepted. Typology accuracy improved from 0% to a few percent; the 25% target was not met and is not reachable with the current tools.

## Context
`full-100-v1` scored 0% typology accuracy: the prompt said to use null unless the pattern was clearly visible, the field was free text, and the agent named a pattern for only 4 of 45 escalated true alerts (all wrong). The rule that fired does not reveal the typology (in eval-100 `high_risk_counterparty` triggers 4-6 true alerts of every typology), so the agent has to infer it from tool output.

## Method
- **Held-out split.** `eval/datasets/tune-100.json` (seed 7, same stratification, built with `--exclude eval/datasets/eval-100.json`; a unit test checks the two sets are disjoint). Prompt iterations were scored on it. `eval-100` was run once at the end. It had already been seen in aggregate (and a few miss rationales), so it is a confirmation set, not a pristine test set.
- **Feasibility first** (`scripts/typology_feasibility.py`, no LLM): the structural signals the tools show, per true typology of `tune-100`, median (min-max):

| typology | n | txns | out | in | counterparties | hop-2 nodes | cycles |
|---|---|---|---|---|---|---|---|
| FAN-OUT | 6 | 8 (1-24) | 8 (0-18) | 1 (0-6) | 6 (1-11) | 6 (2-12) | 0 |
| FAN-IN | 6 | 7 (1-22) | 3 (0-19) | 4 (0-7) | 5 (1-12) | 2 (0-12) | 0 (0-5) |
| CYCLE | 6 | 8 (6-9) | 3 (2-4) | 6 (3-7) | 4 (3-5) | 6 (3-12) | 0 |
| SCATTER-GATHER | 5 | 4 (2-15) | 2 (0-11) | 2 (0-7) | 3 (2-8) | 4 (2-10) | 0 |
| GATHER-SCATTER | 6 | 2 (1-35) | 0 (0-30) | 1 (0-5) | 2 (1-15) | 4 (0-10) | 0 |
| STACK | 5 | 3 (1-20) | 2 (0-16) | 2 (1-7) | 3 (1-10) | 1 (0-6) | 0 |
| BIPARTITE | 6 | 3 (1-9) | 1 (0-3) | 2 (0-6) | 2 (1-4) | 4 (0-14) | 0 (0-1) |
| RANDOM | 5 | 4 (3-5) | 2 (1-3) | 1 (1-3) | 3 (1-3) | 1 (0-6) | 0 (0-1) |

  Reading: only FAN-OUT separates (outgoing-heavy, 4+ counterparties). FAN-IN is only weakly incoming-heavy. CYCLE never shows a cycle through the account in the alert's window, so a "cycles > 0" rule would never fire for it. The other five have a small footprint (2-4 transactions) that overlaps completely. The alert is one account's local slice of a multi-account pattern; a prompt cannot recover structure the tools do not show.
- **Iterations** (each about $1.75): v0 = old prompt (baseline), v1 = closed set plus a structural definition of every pattern, v2 = v1 plus code-counted `STRUCTURE FACTS` (outgoing/incoming counts, counterparties, `flow`, second-hop and cycle counts; `routing.structure_facts`, so the model does no counting) and instructions to use null unless a description is clearly met.

## Results

| run | set | recall | typology accuracy | named (of escalated true) | right when named |
|---|---|---|---|---|---|
| v0 old prompt | tune-100 | 94.0% (84-98%) | 0.0% | 21% (10/47) | 0% |
| v1 definitions only | tune-100 | 94.0% | 2.4% | 57% (27/47) | 4% (1/27) |
| v2 facts + null default | tune-100 | 92.0% (81-97%) | 7.3% | 34% | 21.4% |
| v1 (old prompt) | eval-100 | 90.0% (79-96%) | 0.0% | 9% | 0% |
| **v2, confirmation** | eval-100 | **88.0% (76-94%)** | **2.6%** | 20.5% | 12.5% |

- v1 raised the named rate but names were almost all wrong (26 of 27): with only definitions the model guesses. v2 trades coverage for precision.
- Correct names in v2: tune-100 had 3 (FAN-OUT twice, FAN-IN once), eval-100 had 1 (FAN-OUT). FAN-OUT was right every time it was named; that matches the feasibility table, where it is the one separable pattern.
- Cost and latency unchanged (mean $0.0174 per case, 9.9 s); grounding 100%.
- Recall fell 2 points on both sets. Intervals overlap and one or two alerts differ, so it may be noise, but it is not shown to be. The F15 gate (80%) passes.
- Total spend on LLM runs for F19: about $7.

## Decisions
- `Verdict.typology` is a closed `Literal` of the 8 IBM names or null. Verdicts stored before this change (graph state, checkpoints) carry free text; `Verdict.from_state` maps them with `normalize_typology` so a case resuming after a deploy does not fail.
- Structure facts are computed in Python and shown to the model as non-citable context; they are derived from evidence that is cited.
- The eval report now includes "Named", "Right when named" columns (the header already had them but the rows did not) and a typology confusion table.
- `eval/fixtures/recorded_run.json` (replay gate) now holds `full-100-v2`; thresholds are unchanged.

## Not done, and why
- No new tools or network-structure features. Naming the other five patterns would need information about the whole laundering attempt (other accounts' flows), which is a new feature. It would be a feature of its own, evaluated on a fresh split.
- No further prompt iterations: with n = 50 true alerts a few extra correct names is indistinguishable from noise, and a third iteration on `tune-100` would start overfitting it.
- Recall was not recovered. If the 2-point drop matters, test it with a larger set first.

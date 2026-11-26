# F18 – Portfolio polish

## Goal
Make the repo readable in five minutes by a hiring manager or reviewer: a README that leads with measured results, an architecture diagram that matches the code, a decision index, and a short demo. Nothing here changes runtime behaviour.

## In scope
- **README rewrite** (replaces the 19-line stub). Order: one-paragraph pitch; headline result (agent vs rules-only chart and table from `eval/reports/full-100-v1.md`); architecture diagram; what the agent does (graph, tools, guardrails); run locally; evaluation; load test; AWS; design decisions (links to ADRs); limitations; repo map.
- **Results, taken from committed artifacts only:** eval (`eval/runs/full-100-v1.json`, chart copied to `docs/img/`), load test (`loadtest/results/example-full-1000.md`, sweep table, drills from ADR 0009), cost per case. Every number in the README names its source file and run id.
- **Honest limitations section:** typology accuracy is 0% (prompt tells the model to use null; not a scorer bug); thresholds were set on the same 100 alerts, so the gate is a regression floor, not a generalisation claim; one model pair evaluated; load numbers use a fake LLM on a laptop; AWS stack code-complete but not applied to a real account (update if that changes); console (F14) is a separate repo and not built.
- **Architecture diagram:** one Mermaid system diagram (components + data flow) and one LangGraph diagram (nodes, routing, interrupt), in `docs/architecture.md`, rendered by GitHub. The existing `docs/architecture-aws.md` stays and is linked.
- **ADR index** `docs/adr/README.md`: one line per ADR with status, plus a "decisions in one table" view. Fix stale statements found while doing it (e.g. README says the API has no auth; F11 added it).
- **Docs hygiene:** `docs/features/` index with status per feature F00–F18; CLAUDE.md status line updated; `.env.example` and the quick-start commands verified by running them from a clean checkout.
- **Demo:** a scripted, reproducible walkthrough `docs/demo.md` (commands + expected output) and a 3–4 minute screen recording linked from the README. The script uses only the API/CLI (see Decisions).
- `docs/features/F18-explained.txt` (project convention).

## Out of scope
New features, prompt or threshold tuning, rerunning the real-LLM eval, applying Terraform, building the F14 console, a docs site (MkDocs), publishing a blog post.

## Interfaces
No code interfaces. Deliverables: `README.md`, `docs/architecture.md`, `docs/img/*`, `docs/adr/README.md`, `docs/features/README.md`, `docs/demo.md`, `docs/features/F18-explained.txt`.

## Rules to keep
- No number without a source in the repo; no claim stronger than the evidence (report CIs as the eval report does).
- Do not copy secrets, account IDs, ALB hostnames or personal paths into docs or screenshots.
- Docs only: if a command in the README fails, fix the doc or open a separate fix, don't change app code in this branch.
- `eval/reports/` is gitignored: copy the chart/table into `docs/img/` and `docs/results/` rather than linking ignored paths.

## Acceptance criteria
- A fresh clone follows "Run locally" to a working `/health` and a passing `pytest` without undocumented steps (verified once, output noted in the PR).
- Every relative link in `README.md` and `docs/**/*.md` resolves (checked by a small script or `lychee`); Mermaid blocks render on GitHub.
- Each figure in the README matches its source artifact (spot-checked by a test that parses `eval/runs/full-100-v1.json` and compares the headline numbers, so a re-run can't silently drift from the README).
- ADR index lists all 11 ADRs; features index covers F00–F18.
- Demo script runs end to end on the local stack; recording linked.

## Decisions (proposed, confirm before building)
1. **Demo medium:** the console is a separate, unbuilt repo, so record the demo against Swagger UI + terminal + a Postgres/SSE view. Alternative: build the F14 console first. Recommended: API/terminal demo now, add console footage later.
2. **Results freshness:** use the committed `full-100-v1` run as is (no new spend). Alternative: fix the typology prompt on a held-out split and rerun (~$2) so the headline isn't 0%. Recommended: ship as is with the limitation stated, and treat the typology fix as its own feature.
3. **AWS claims:** say "code complete, validated by `terraform validate`/CI, not applied" unless you run the apply first and add real numbers. Recommended: honest wording now.
4. **Diagram format:** Mermaid in markdown (renders on GitHub, diffable) vs exported PNG/SVG. Recommended: Mermaid, plus the PNG chart for the eval.

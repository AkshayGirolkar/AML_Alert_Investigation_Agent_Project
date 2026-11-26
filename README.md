# AML Alert Investigation Agent

A deterministic SQL rules engine raises anti-money-laundering alerts on the IBM synthetic AML dataset. A LangGraph agent investigates each alert, closes it or escalates it with a cited, grounded verdict, and a human analyst approves escalations. Every verdict is scored against the dataset's ground-truth labels and compared with a rules-only baseline.

## Headline result

100 alerts (50 truly laundering, 50 false), frozen and stratified (`eval/datasets/eval-100.json`). Planner `gpt-4o-mini`, assessor `gpt-4o`. Run `full-100-v2` (current prompt); full report in [`docs/results/eval-full-100-v2.md`](docs/results/eval-full-100-v2.md), raw rows in [`docs/results/eval-full-100-v2.json`](docs/results/eval-full-100-v2.json). The earlier prompt's run is kept as [`eval-full-100-v1`](docs/results/eval-full-100-v1.md).

![Agent vs rules-only](docs/img/eval-agent-vs-rules.png)

| | Rules only (escalate every alert) | Agent |
|---|---|---|
| Recall of true laundering alerts | 100.0% | 88.0% (95% CI 76-94%) |
| Precision of escalations | 50.0% | 58.7% (47-69%) |
| Alerts sent to an analyst | 100 | 75 |
| Launderers missed | 0 | 6 |
| False alerts closed | 0 | 19 |

The agent removes 25.0% of the analyst workload and lets 12.0% of true alerts through. Whether that trade is acceptable is a policy choice, not something this project decides. Grounding rate is 100.0% (every verdict cites evidence ids that exist in state), mean cost $0.0174 per case, mean latency 9.9 s.

### Limitations (read these before quoting the numbers)
- **Typology accuracy is about 3%.** The first prompt scored 0% (it rarely named a pattern and was wrong when it did). F19 made the typology a closed set of the IBM names and added counted flow facts (outgoing vs incoming, counterparties, cycles) to the prompt. That raised it to 7.3% on a separate tuning set and 2.6% on this set (1 of 39 escalated true alerts). It stays low because the tools show one account's local footprint, which does not separate most IBM patterns (feasibility table in [ADR 0012](docs/adr/0012-f19-typology-prompt.md)). The one correct name here is a FAN-OUT, and every FAN-OUT it named was right; it names any pattern for about 20% of escalated true alerts. The 25% target was not met.
- **Recall moved from 90.0% to 88.0% with the new prompt** (94% to 92% on the tuning set). The intervals overlap heavily, so this may be noise, but it was not shown to be.
- **The eval thresholds are a regression floor, not a generalisation claim.** `eval/thresholds.json` was set from the same 100 alerts. Tuning prompts against that set would overfit it; a held-out split must come first.
- **One model pair and one run.** Confidence intervals are wide (n = 50 true alerts); per-typology rows have 5-6 alerts each.
- **Load numbers use a fake LLM on a laptop.** See below.
- **AWS:** the Terraform is code-complete and validated in CI but has not been applied to a real account, so there are no deployed-stack numbers.
- **The analyst console (F14) is a separate project and is not built.** Analysts use the API (Swagger UI at `/docs`).

## Architecture

```mermaid
flowchart LR
  rules[SQL rules engine] -->|alerts| PG[(Postgres + pgvector)]
  client([Analyst / API client]) -->|JWT| API[FastAPI]
  API -->|enqueue| SQS[[SQS + DLQ]]
  SQS --> W[Worker: LangGraph graph]
  W --> PG
  W --> R[(Redis)]
  W --> LLM[LLM gateway: OpenAI]
  API --> R
  API --> PG
  W -.traces, PII masked.-> LS[LangSmith]
```

More detail, including the graph and the AWS topology: [`docs/architecture.md`](docs/architecture.md), [`docs/architecture-aws.md`](docs/architecture-aws.md).

### What the agent does
`load_alert → load_kyc → recall_history → plan → tool_loop (max ~8 calls) → assess → validate_grounding → close_case | draft_sar → human_review (interrupt) → write_memory`

- **Tools:** transaction query, 2-hop counterparty network (recursive CTE), sanctions screen (trigram match, Redis-cached), adverse-media search (pgvector), peer comparison.
- **Guardrails:** Pydantic verdicts; a deterministic grounding check rejects any cited evidence id that is not in state (one retry); the LLM never does arithmetic (SQL/Python computes); a prior analyst-approved escalation blocks auto-close; per-case cost budget sends over-budget cases to human review.
- **Durability:** SQS with DLQ, `PostgresSaver` checkpoints, idempotent side effects. A killed worker's cases resume from their checkpoints.
- **Memory:** long-term per-customer history in `PostgresStore`, used as context and never as evidence.
- **Auth:** JWT (Cognito-compatible), analyst and supervisor roles, audit log on every decision.

## Load test (fake LLM, everything else real)

1,000 real investigations through API, SQS, worker, graph, Postgres and Redis, with the LLM replaced by a deterministic stand-in so the run measures this system and not OpenAI. One laptop, Docker VM with 8 CPUs and 4 GB.

| | |
|---|---|
| Cases finished | 1000 / 1000 in 215 s |
| Throughput (steady state) | 287 cases/min (was 40 before fixes) |
| Errors / duplicates / DLQ | 0 / 0 / 0 |

Source: [`docs/results/loadtest-full-1000.md`](docs/results/loadtest-full-1000.md); method, findings and failure drills (worker kill, Redis outage, 10% LLM errors, all passed) in [ADR 0009](docs/adr/0009-f16-load-test.md). Two real bottlenecks were found and fixed: an unindexed alias scan in sanctions screening (3.96 s to 0.06 s per call) and a too-small worker DB pool. With the real OpenAI API the ceiling is the configured rate limiter, about 130 cases/min at the default setting.

## Run locally
```bash
uv venv --python 3.12 && source .venv/bin/activate
uv pip install -r requirements-dev.txt
cp .env.example .env                # add OPENAI_API_KEY for real runs
docker compose up -d --build        # postgres, redis, localstack, migrate, api (:8000), worker
curl localhost:8000/health
pytest
```
Walkthrough of an alert through investigation and approval: [`docs/demo.md`](docs/demo.md). The data load (Kaggle HI-Small, ~5M rows) is described in [`docs/features/F02-ingestion.md`](docs/features/F02-ingestion.md).

Re-score without spending money: `python -m scripts.eval_gate --mode replay`. A live run (`python -m scripts.eval_run`) costs about $2 per 100 alerts.

## Deploy to AWS
Terraform in `infra/` builds the VPC, ECS Fargate services (API, worker), RDS Postgres, ElastiCache Redis, SQS + DLQ, Secrets Manager, CloudWatch alarms and autoscaling, with optional Cognito. See `infra/README.md`, [`docs/runbook-aws.md`](docs/runbook-aws.md) and [ADR 0010](docs/adr/0010-f17-aws-terraform.md). Estimated cost is about $6-7 per day while it runs (mostly NAT, RDS, Redis, ALB, Fargate), so `terraform destroy` after each session. The dev ALB only admits the IPs you list, and API auth is `AUTH_MODE=jwt` (off by default for local work).

## Decisions and docs
- [ADR index](docs/adr/README.md): why each major choice was made.
- [Feature index](docs/features/README.md): specs F00-F18 and a plain-language `Fxx-explained.txt` for most.
- Original design: `AML_Alert_Investigation_Agent_Project.md`. Contributor and Claude Code notes: `CLAUDE.md`.

## Repo map
`app/` agent, API, worker, rules, eval, loadtest, services · `alembic/` migrations · `scripts/` loaders, eval, load test, AWS helpers · `eval/` frozen dataset, thresholds, fixtures · `infra/` Terraform · `tests/` unit and integration · `docs/` specs, ADRs, results.

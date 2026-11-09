## Project: AML Alert Investigation Agent

**My pick, and why:** the IBM synthetic AML dataset on Kaggle comes with **ground-truth labels and named laundering patterns**. That’s rare. You can measure exactly how well your agent performs, which is the strongest thing a portfolio project can show. The project also uses every technology on your list for a genuine reason, not as decoration.

### Data sources

1. **IBM “Transactions for Anti Money Laundering (AML)”** (Kaggle). Start with the `HI-Small` variant, about 5M transactions. Each row has timestamp, sender/receiver bank and account, amounts, currencies, payment format, and an `Is Laundering` label. The companion `HI-Small_Patterns.txt` file lists each laundering attempt by typology (fan-in, fan-out, cycle, scatter-gather, and others).
2. **OpenSanctions** (opensanctions.org): real sanctions and PEP (politically exposed persons) lists as bulk downloads. Free for non-commercial use, which covers a portfolio.
3. **Synthetic data you generate:** 
   - KYC profiles for each account (Faker: name, occupation, country, expected monthly volume, risk rating).
   - Deliberately match a few names to OpenSanctions entries.
   - Adverse-media articles about about 2% of customers (LLM-generated), so the agent has something to find.

### How it works

1. **Load the data.** Load the transactions into Postgres.
2. **Monitor transactions.** A deterministic rules engine (SQL) flags suspicious activity: high velocity, fan-out, round-tripping, amounts just under reporting limits, new high-risk counterparties. These rules generate **alerts**, mostly false positives, just like in real banks.
3. **Investigate.** The **agent** investigates each alert, decides whether to *close* (false positive) or *escalate* (draft a suspicious activity report), and cites evidence for every claim.
4. **Review.** A human analyst approves escalations and high-risk closures.
5. **Score.** You compare every verdict against the `Is Laundering` labels.

### Architecture

```
 Analyst console (Streamlit v1 → React later)
        │ HTTPS + JWT
   FastAPI (api)  ── Redis: rate limits, SSE pub/sub, case locks
        │
   SQS (+DLQ)   [LocalStack locally]
        ▼
   Worker: LangGraph investigation graph
     ├─ LangChain tools → Postgres (transactions, KYC)
     ├─ sanctions_screen → Redis cache → OpenSanctions table
     ├─ adverse_media_search → pgvector
     └─ LLM gateway (OpenAI primary, Bedrock/Anthropic fallback)
        │
   PostgreSQL + pgvector
     - transactions (partitioned by month), accounts, kyc_profiles
     - alerts, cases, evidence, decisions, audit_log
     - LangGraph checkpoints (PostgresSaver) = short-term memory
     - PostgresStore = long-term memory
   LangSmith (traces, datasets, evals) + CloudWatch metrics/alarms
```

### LangGraph investigation graph

1. `load_alert` → `load_kyc_profile` → `recall_history` (long-term memory: “this customer was investigated twice before and cleared for payroll activity”)
2. `plan_investigation`: structured output listing which checks to run
3. A bounded tool loop (max about 8 tool calls), with each result appended to an `evidence` list in state. Tools: 
   - `query_transactions`
   - `counterparty_network` (recursive CTE, 2 hops)
   - `sanctions_screen`
   - `adverse_media`
   - `peer_comparison`
4. `assess`: Pydantic verdict `{disposition, typology, risk_score, confidence, evidence_ids}`
5. `validate_grounding`: deterministic check that every cited evidence ID exists in state
6. Routing: 
   - Low risk with high confidence → `close_case`
   - Otherwise → `draft_sar` → `interrupt()` for analyst review → resume via `POST /cases/{id}/decision`
7. `write_memory`: store the outcome and the analyst’s feedback in long-term memory

### Where each technology earns its place

- **LangGraph:** bounded loops, conditional routing, durable human-in-the-loop interrupts, checkpointing.
- **LangChain:** tool definitions, model abstraction for provider fallback, structured outputs.
- **FastAPI:** async endpoints, dependency-injected auth and RBAC (analyst vs. supervisor roles), SSE progress streaming.
- **PostgreSQL + SQLAlchemy 2.0 (async) + Alembic:** 
  - Millions of rows, so you’ll learn indexing, partitioning, and `EXPLAIN ANALYZE`.
  - Row locks so two workers never take the same case.
- **pgvector:** adverse media search and “similar past cases” retrieval.
- **Redis:** 
  - Sanctions screening cache with TTL.
  - Distributed token-bucket limiter shared by all workers (keeps you under OpenAI rate limits).
  - Case locks and SSE pub/sub.
- **Memory:** short-term memory is the per-case checkpointed thread. Long-term memory holds per-customer investigation history and analyst corrections.
- **LangSmith:** traces for every investigation, an eval dataset of labeled alerts, and experiment comparison when you change prompts or models.
- **Docker Compose (local):** api, worker, postgres (pgvector image), redis, localstack, plus a seed/loader job.
- **AWS:** 
  - Compute and networking: ECS Fargate (api and worker services), ALB, ECR.
  - Data: RDS PostgreSQL, ElastiCache Redis, S3 (raw data, report PDFs).
  - Messaging and auth: SQS + DLQ, Cognito.
  - Operations: Secrets Manager, CloudWatch (logs, queue-depth autoscaling for workers).
  - Infrastructure as code: Terraform.
- **CI/CD (GitHub Actions):** lint and type-check, pytest with Testcontainers for Postgres and Redis, an **eval gate** on every PR (100 labeled alerts; fail if recall drops), then build → ECR → ECS deploy.
- **Guardrails:** Pydantic validation, the grounding check, and PII masking before traces are sent to LangSmith.

### Evaluation (the headline of your README)

Report these per typology:

- **Recall on true laundering alerts.** Most important: a missed launderer is the costly error.
- **False closure rate** and precision of escalations.
- **Typology accuracy:** does the agent name the same pattern the IBM patterns file does?
- **Grounding rate:** share of narrative claims backed by valid evidence IDs.
- **Cost and latency per case,** compared across models (e.g., GPT-4o vs. a smaller model for planning).

The “agent vs. rules-only baseline” comparison is the chart hiring managers will remember.

### Build plan (about 8 weeks, evenings/weekends longer)

1. **Weeks 1–2:** 
   - Data loader, schema and partitioning, synthetic KYC and media.
   - Rules engine generating alerts.
   - FastAPI skeleton, Docker Compose.
2. **Weeks 3–4:** 
   - LangGraph graph with tools, checkpointing, structured verdicts.
   - SQS worker flow.
3. **Week 5:** 
   - Human-in-the-loop interrupts and the analyst console.
   - Long-term memory, Redis caching and rate limiting.
4. **Week 6:** LangSmith eval dataset, metrics, CI eval gate.
5. **Week 7:** Terraform AWS deployment, Cognito, CloudWatch alarms, autoscaling.
6. **Week 8:** 
   - Load test (e.g., 1,000 alerts through the queue) and fix bottlenecks.
   - Write the README, architecture decision records, and demo video.

**Cost tip:** RDS, ElastiCache, and especially NAT gateways bill hourly. Develop locally with Docker Compose, deploy to AWS for testing and the demo, and run `terraform destroy` when you’re done. Set a billing alarm on day one.

I can also make this a doc you can edit here, share, or download as Word or PDF, if you’d like.

thanks, I want to build this using claude code, can you tell me in step by step basis what should I do, can you break it down to features? would that be good approach?

Yes, breaking it into features is the right approach, with two conditions:

- **Build in dependency order,** so each feature runs on top of working, tested code. Don’t build the agent before the data and database exist.
- **Keep each feature small,** about 1–3 days of work with clear “done when” criteria. Claude Code does excellent work on well-scoped tasks and drifts on vague, huge ones (“build the AML system”).

Treat yourself as the architect and reviewer, and Claude Code as the implementer. That also means you’ll understand every line well enough to explain it.

### One-time setup

1. **Create the repo** and a folder structure: `app/` (api, agent, db, services), `workers/`, `scripts/`, `tests/`, `infra/`, `docs/`.
2. **Write `docs/architecture.md`** by pasting the architecture from my previous answer.
3. **Create a `CLAUDE.md`** in the repo root (you can start with `/init`, then edit it). Claude Code reads this file every session, so put your standing rules here: 
   - Stack and versions: Python 3.12, FastAPI, SQLAlchemy 2.0 async, Pydantic v2, LangGraph, uv for dependencies.
   - Commands: how to run tests, lint (ruff), type-check (mypy), and start Docker Compose.
   - Conventions: 
     - Type hints everywhere and tests required for every feature.
     - Never let the LLM do arithmetic, and never commit secrets (use `.env`).
     - All side-effecting tools must be idempotent.
   - Workflow: “Read `docs/features/<feature>.md` before starting; don’t change code outside the feature’s scope.”
4. **Create `docs/features/`** with one spec file per feature (template below).

### The loop for every feature

1. **Write the spec** (`docs/features/F06-agent-graph.md`): goal, in scope, out of scope, interfaces, and acceptance criteria. Claude can draft it, but you edit it.
2. **Start fresh.** Begin a new session or run `/clear` so old context doesn’t leak in, then use **plan mode**: “Read the spec and CLAUDE.md, propose an implementation plan. Don’t write code yet.” Push back on the plan until it’s right.
3. **Write tests first.** Have Claude write tests from the acceptance criteria and review them yourself. This is where your QA background is a real advantage: weak tests mean weak code.
4. **Implement** until tests, ruff, and mypy pass.
5. **Review the diff yourself.** Ask “explain why you did X” for anything unclear. Watch for tests that mock away the very thing they should test.
6. **Commit to a feature branch, open a PR, and let CI pass** before merging.
7. **Record decisions** in an ADR (`docs/adr/`) and update CLAUDE.md if a new convention emerged.

### Feature breakdown (in build order)

**Phase 0: Foundation**

- **F00 Project scaffold.** FastAPI app, pydantic-settings config, Docker Compose (pgvector Postgres, Redis, LocalStack), `/health` endpoint, ruff/mypy/pytest, GitHub Actions CI.
   *Done when:* `docker compose up` works and CI is green.
- **F01 Database layer.** Async SQLAlchemy models, Alembic migrations, schema (accounts, partitioned transactions, kyc_profiles, alerts, cases, evidence, decisions, audit_log).
   *Done when:* migrations run up and down cleanly, and repository tests pass.

**Phase 1: Data**

- **F02 Transaction ingestion.** Kaggle HI-Small loader using Postgres `COPY`, plus a parser for the patterns file into a ground-truth table.
   *Done when:* about 5M rows load in minutes, and the row counts match the source files.
- **F03 Synthetic KYC, sanctions, and media.** Faker profiles, OpenSanctions import with a few planted name matches, LLM-generated adverse media embedded into pgvector.
   *Done when:* data generation is deterministic (seeded) and reproducible.
- **F04 Rules engine.** SQL detection rules that produce alerts, each linked to its ground-truth label.
   *Done when:* alerts are generated and you can report the rules-only baseline precision and recall.

**Phase 2: Agent core**

- **F05 Agent tools.** LangChain tools (transaction query, counterparty network, sanctions screen, adverse media, peer comparison), each a tested pure function.
   *Done when:* every tool has unit tests and handles empty or malformed results.
- **F06 LangGraph graph v1.** Plan → bounded tool loop → structured verdict → grounding validation → route. Run synchronously from a CLI.
   *Done when:* one alert can be investigated end-to-end from the CLI.
- **F07 Async execution and checkpointing.** API enqueues to SQS, worker consumes, PostgresSaver checkpoints, idempotency, DLQ.
   *Done when:* killing a worker mid-run and restarting it resumes the case.
- **F08 Human-in-the-loop.** `interrupt()`, a decision endpoint, and resume.
   *Done when:* an analyst’s approval or rejection correctly resumes the graph.
- **F09 Long-term memory.** PostgresStore for customer history and analyst feedback.
   *Done when:* a second alert on the same customer uses the prior outcome.

**Phase 3: Production hardening**

- **F10 Redis.** Sanctions cache, distributed token-bucket LLM rate limiter, case locks, SSE progress streaming.
- **F11 Auth and RBAC.** JWT (Cognito-compatible), analyst and supervisor roles, audit log on every decision.
- **F12 LLM gateway.** Provider fallback (OpenAI → Anthropic/Bedrock), timeouts, retries with backoff, per-case token and cost tracking.
- **F13 Observability.** LangSmith tracing with PII masking, structured JSON logs, metrics (queue depth, latency, cost).
- **F14 Analyst console.** Streamlit UI: alert queue, case view with evidence, approve/reject.

**Phase 4: Quality**

- **F15 Evaluation harness.** LangSmith dataset of labeled alerts, metrics (recall, false-closure rate, typology accuracy, grounding, cost), comparison against the rules-only baseline, and a CI eval gate on PRs.
- **F16 Load test.** Locust or a script pushing 1,000 alerts through the queue; find and fix bottlenecks.

**Phase 5: Cloud**

- **F17 AWS with Terraform.** VPC, ECS Fargate (api and worker), RDS, ElastiCache, SQS, S3, Secrets Manager, Cognito, CloudWatch, and a deploy pipeline from GitHub Actions.
- **F18 Portfolio polish.** README with eval results, ADRs, architecture diagram, demo video.

### Spec template

markdown

```markdown
# F06 – LangGraph Investigation Graph v1
## Goal
Investigate a single alert and produce a grounded verdict.
## In scope
State schema, nodes (load_alert, load_kyc, plan, tool_loop, assess,
validate_grounding, route), CLI: `python -m app.agent.run --alert-id X`
## Out of scope
SQS, checkpointing, HITL, memory (later features)
## Interfaces
Verdict = Pydantic {disposition, typology, risk_score, confidence, evidence_ids}
## Acceptance criteria
- Tool loop stops at max 8 calls
- Verdicts citing unknown evidence IDs are rejected and retried once
- Unit tests for routing logic with a fake LLM; one integration test on a real alert
```

### Tips

- **Develop on a slice of data.** Use about 100k transactions while building and the full dataset only for evals and load tests.
- **Use current docs, not memory.** LangGraph’s APIs change quickly, so tell Claude Code to check the current docs (or paste the relevant page) rather than relying on what it remembers. Pin versions in your lockfile.
- **One feature per session.** Long sessions accumulate stale context.
- **Keep secrets out of the repo.** Put API keys in `.env`, gitignore it, and never paste them into prompts or commit them.
- **Don’t skip F15.** The evaluation harness is the most convincing part of the whole project.

The official Claude Code docs at [https://docs.claude.com/en/docs/claude-code/overview](https://docs.claude.com/en/docs/claude-code/overview) cover CLAUDE.md, plan mode, and custom slash commands. A custom command for your per-feature loop would save you retyping the same instructions each time.

create a requirements.txt file content to install all necessary libraries

I made two files. `requirements.txt` holds the runtime libraries and goes into your Docker image. `requirements-dev.txt` includes the runtime file plus the testing, linting and load-testing tools, which keeps the production image smaller.

To install everything locally:

bash

```bash
uv venv && source .venv/bin/activate
uv pip install -r requirements-dev.txt
python -m spacy download en_core_web_lg   # language model Presidio needs for PII detection
```

A few things you’ll need to do:

- **Lock exact versions.** The files use minimum versions only, because LangChain and LangGraph release quickly and I can’t confirm the latest versions from here. After your first install, run `uv pip compile requirements.txt -o requirements.lock`, and have your Dockerfile and CI install from that lock file. That way every build is reproducible.
- **Set up Kaggle access.** Create an API token in your Kaggle account settings and save it as `~/.kaggle/kaggle.json` before running the dataset download.
- **Add API keys to `.env`.** You’ll need `OPENAI_API_KEY`, `LANGSMITH_API_KEY` and `LANGSMITH_TRACING=true`, plus `ANTHROPIC_API_KEY` if you want the fallback provider. Keep `.env` out of git.
- **Things you don’t need to install:** pgvector itself runs inside the `pgvector/pgvector` Postgres Docker image, and LocalStack runs as a container from Docker Compose.

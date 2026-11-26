# CLAUDE.md
This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

# AML Alert Investigation Agent
Project overview and architecture:
@AML_Alert_Investigation_Agent_Project.md

## Status

In progress. F00–F08 have been started or built (app code, Alembic migrations, ingestion, synthetic data in `app/synth/`). The repo is under git (feature branches, PRs into `main`). Use the single local venv `.venv/` (Python 3.12, gitignored); don't create other venvs.

Full design (data sources, architecture, LangGraph graph, feature breakdown F00–F18, eval plan, build order):

@AML_Alert_Investigation_Agent_Project.md

## Things that will trip you up

- **Requirements are partial and unlocked.** `requirements.txt` = runtime libs added feature by feature (so far SQLAlchemy/asyncpg/psycopg/Alembic/pydantic/pgvector/Faker/openai/FastAPI/LangGraph/redis); `requirements-dev.txt` = `-r requirements.txt` plus pytest, ruff, mypy, pre-commit, testcontainers, locust. FastAPI, LangGraph/LangChain, Redis, boto3, Presidio etc. are not listed yet; add each with the feature that needs it. There is no `requirements.lock` yet; the plan is a compiled lock used by Docker and CI.
- **Python version:** `.venv` is Python 3.12, matching the design (uv for dependencies). Don't switch to 3.14; LangChain/LangGraph/Presidio may not support it.
- **Env files:** `.env` holds real secrets (gitignored); `.env.example` is committed with placeholders. Update `.env.example` whenever a new variable is added.
- Presidio needs `python -m spacy download en_core_web_lg` after install.

## Architecture in one paragraph

IBM synthetic AML transactions (HI-Small, ~5M rows, with `Is Laundering` ground truth and a `HI-Small_Patterns.txt` typology file) load into partitioned Postgres via `COPY`. A deterministic SQL rules engine generates alerts. FastAPI enqueues alerts to SQS; a worker runs a LangGraph investigation graph (load alert/KYC → recall memory → plan → bounded tool loop, max ~8 calls → Pydantic verdict → deterministic grounding check → close or `draft_sar` → `interrupt()` for analyst approval → write memory). Checkpoints use `PostgresSaver` (short-term) and `PostgresStore` (long-term). Redis handles the sanctions cache, a shared LLM token-bucket limiter, case locks, and SSE pub/sub. Verdicts are scored against the ground-truth labels and compared with a rules-only baseline.

## Non-obvious rules from the design

- Never let the LLM do arithmetic; compute in SQL/Python.
- Every verdict must cite evidence IDs that exist in graph state; `validate_grounding` rejects unknown IDs (retry once).
- All side-effecting tools must be idempotent (SQS redelivery, resume after crash).
- Mask PII before traces go to LangSmith.
- Check current LangGraph/LangChain docs rather than relying on memory; pin versions in the lockfile.
- Develop against a ~100k-transaction slice; use the full dataset only for evals and load tests.
- Work one feature at a time from `docs/features/<feature>.md` and don't change code outside that feature's scope.

## Commands

```bash
source .venv/bin/activate            # .venv already exists (Python 3.12)
uv pip install -r requirements-dev.txt
docker compose up -d postgres        # only service defined so far (pgvector pg17, host port 5433)
alembic upgrade head                 # schema migrations
python -m scripts.load_hi_small      # F02 ingestion; scripts/ also has generate_kyc, generate_media, load_sanctions, plant_sanctions_matches (run with -m)
pytest tests/path/test_x.py::test_name   # single test; `live` marker (real OpenAI) is skipped by default, use -m live
ruff check . && mypy .
```

F07 (async): `docker compose up -d --build` runs postgres, localstack (SQS), migrate, api (:8000) and worker; `python -m app.worker` runs the worker locally, `uvicorn app.main:app` the API. Everything in `app/agent` is async now. SQS integration tests need LocalStack (`docker compose up -d localstack`) and are skipped otherwise. `requirements.lock` (uv compile) is what the Dockerfile installs: regenerate it when `requirements.txt` changes.

F08 (human review): escalated cases pause at `human_review` (`interrupt()`), status `awaiting_review`; `POST /cases/{id}/decision` records the decision (`decisions`, `audit_log`) and sends a `resume` SQS message that the worker turns into `Command(resume=...)`. `python -m app.agent.run --alert-id N --decision approve` demos it with an in-memory checkpointer. No auth yet (F11), so `analyst_id` is in the request body.

F09 (long-term memory): `recall_history` (after `load_kyc`) and `write_memory` (end of both the close and review paths) use an `AsyncPostgresStore` (tables from `store.setup()`, like the checkpointer). Records live under `("customer_history", entity_id)`, key `alert-{id}`; history goes into prompts as context, never evidence, and a prior analyst-approved escalation blocks auto-close (`memory_block_auto_close_on_prior_sar`). `build_graph(..., store=None)` makes memory a no-op. The CLI forgets between runs unless you pass `--memory-pg`. See `docs/adr/0003-f09-long-term-memory.md`.

F10 (Redis): `docker compose up -d redis`. `app/services/` holds `sanctions_cache` (TTL + `sanctions:version`, bumped by `load_sanctions`), `rate_limiter` (Lua token bucket, wrapped by `RateLimitedLLM`), `locks` (`lock:case:{id}`) and `events` (stream `case:{id}:events`, served at `GET /cases/{id}/events`). Redis is never required: every use degrades. Unit tests use `fakeredis`; `tests/integration/test_redis.py` needs a real Redis. See `docs/adr/0004-f10-redis.md`.

F10.5 (console contract): a separate console (React + Node, built later in F14's slot, see ADR 0005) shares only the database. Migration 0006 adds schema `console` (views `v_alert_queue`, `v_case_detail`, `v_alert_transactions` (0007), `v_requests`; table `console.requests`; role `console_app`) and `cases.evidence`. `app/worker/console_relay.py` (a task in the worker, `CONSOLE_RELAY_ENABLED`) applies request rows through `app/services/case_commands.py`, the same code the API uses. The console never writes `cases`/`decisions`/`audit_log`. The contract for the console team is `docs/console-contract.md`. The planned Streamlit F14 is replaced by that console; F11 (FastAPI JWT) is optional.

F12 (LLM gateway): `app/agent/gateway.py` `GatewayLLM` wraps the OpenAI providers with timeout, backoff retries on transient errors, fallback to a second model on the same key (`LLM_FALLBACK_*_MODEL`), the F10 rate limiter (one token per real call) and a per-case cost budget (`LLM_CASE_BUDGET_USD`; over it the nodes produce no verdict and the case goes to human review). Usage lands in `llm_calls` (migration 0008, view `llm_case_costs`), attributed by `case_scope(case_id)` set in `Processor.handle`. Costs come from `app/agent/pricing.py` (check prices). Run Python via `.venv/bin/python`: the shell's `python` alias points at another venv. See `docs/adr/0006-f12-llm-gateway.md`.

F13 (observability): `app/observability/` has `logging` (JSON logs, `LOG_FORMAT=json`, `bind_case`/`bind_request` context ids), `pii.mask` (pure regex + known-key masker), `tracing.setup_tracing` (LangSmith client created with the masker as `hide_inputs/outputs/metadata`; off unless `LANGSMITH_TRACING=true` + key; refuses a pre-existing unmasked client), `metrics` (in-process Prometheus-text registry) and `scrape` (DB/SQS gauges read at `GET /metrics`). `GET /ready` reports `degraded` for Redis/SQS trouble. The worker serves its own series on `WORKER_METRICS_PORT`. See `docs/adr/0007-f13-observability.md`.

F15 (evaluation harness): `app/eval/` scores the agent against ground truth. `dataset` builds the frozen, stratified alert set (`eval/datasets/eval-100.json`, committed; `python -m scripts.eval_build_dataset`), `runner`/`pipeline` run the graph on each alert (graph input is only `alert_id`; no SQS, no memory store, never resumed past `human_review`) and keep cost via an in-process `MemoryRecorder` because eval has no `cases` rows. `metrics` (pure) gives recall, false-closure, precision, typology accuracy (alias map in `typology.py`), grounding rate, cost/latency with Wilson intervals; `baseline` is "escalate every alert" on the same set; `gate` checks `eval/thresholds.json`. Results go to `eval_runs`/`eval_results` (migration 0009) and `eval/runs/*.json`; reports and chart to `eval/reports/` (gitignored). Scripts: `scripts.eval_run` (real OpenAI, costs money; `--limit` for smoke runs, `--record` to refresh `eval/fixtures/recorded_run.json`), `eval_compare`, `eval_gate` (`--mode replay` default needs no LLM; `--mode live`). Never tune prompts or thresholds against the eval set without first splitting a held-out set. See `docs/adr/0008-f15-evaluation-harness.md`.

F16 (load test): `LLM_PROVIDER=fake` swaps OpenAI for `app/agent/synthetic_llm.py` (deterministic, seeded by alert id, `FAKE_LLM_LATENCY_MS`/`_ERROR_RATE`/`_ESCALATE_PCT`) inside the F12 gateway, so the rate limiter, retries and cost recording still run; the worker and `app.agent.run` then need no OpenAI key. `app/loadtest/` holds pure metrics (`metrics`), enqueue (`enqueue`), DB readback (`collect`), resource sampler (`sampler`), load-set selection and reset (`seed`) and result/summary building (`report`). Scripts: `loadtest_seed` (manifest `loadtest/datasets/load-1000.json`, excludes eval-100 and alerts with cases; `--reset` deletes only its rows), `loadtest_run` (one burst/steady run through the API, writes `loadtest/results/*.json|md`, exit 1 unless drained with 0 duplicates and 0 DLQ), `loadtest_sweep` (restarts the worker per config on the fake LLM and restores the openai worker at the end), `loadtest_drill` (worker kill, Redis down, 10% LLM errors). `docker compose --profile scale up -d worker worker2` runs a second worker; compose passes `LLM_PROVIDER`, `FAKE_LLM_*`, `WORKER_CONCURRENCY`, `WORKER_DB_POOL_SIZE` and the rate-limit settings to the containers. Findings: unindexed alias scan in `sanctions_screen` (fixed, migration 0010 `sanctions_aliases`), worker psycopg pool (`WORKER_DB_POOL_SIZE`, 0 = auto), one worker process saturates a core at ~310 cases/min. Rebuild the image with `docker compose build migrate` (api and worker only reference `aml-app`). See `docs/adr/0009-f16-load-test.md`.

Not built yet: GitHub Actions CI, `pyproject.toml`.

Required env in `.env` (never commit): `OPENAI_API_KEY`, `LANGSMITH_API_KEY`, `LANGSMITH_TRACING=true`, optional `ANTHROPIC_API_KEY`. Kaggle download needs `~/.kaggle/kaggle.json`.

# CLAUDE.md
This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

# AML Alert Investigation Agent
Project overview and architecture:
@AML_Alert_Investigation_Agent_Project.md

## Status

Greenfield. No application code exists yet — only the design doc, `requirements.txt`, and a local venv (`my_venv/`, gitignore it). The folder is not yet a git repo.

Full design (data sources, architecture, LangGraph graph, feature breakdown F00–F18, eval plan, build order):

@AML_Alert_Investigation_Agent_Project.md

## Things that will trip you up

- **`requirements.txt` currently holds only dev/test tooling** (pytest, testcontainers, ruff, mypy, pre-commit, locust). The runtime libraries (FastAPI, SQLAlchemy, LangGraph, etc.) described in the design doc are not listed yet. The plan is `requirements.txt` = runtime and `requirements-dev.txt` = `-r requirements.txt` plus dev tools, with a compiled `requirements.lock` used by Docker and CI.
- **Python version mismatch:** `my_venv` is Python 3.14.7, but the design targets Python 3.12 (uv for dependencies). Confirm before installing; LangChain/LangGraph/Presidio may not support 3.14.
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

## Commands (planned, not yet runnable)

```bash
uv venv && source .venv/bin/activate
uv pip install -r requirements-dev.txt
docker compose up            # postgres (pgvector image), redis, localstack, api, worker
pytest tests/path/test_x.py::test_name   # single test
ruff check . && mypy .
```

Required env in `.env` (never commit): `OPENAI_API_KEY`, `LANGSMITH_API_KEY`, `LANGSMITH_TRACING=true`, optional `ANTHROPIC_API_KEY`. Kaggle download needs `~/.kaggle/kaggle.json`.

# F07 – Async execution, checkpointing, SQS worker

## Goal
Run investigations as a service: the API enqueues an alert to SQS, an async worker runs the LangGraph graph with `AsyncPostgresSaver` checkpoints, and the result is stored on a `cases` row. A killed worker resumes from its checkpoint; redelivery never duplicates work; poison messages go to a DLQ.

## In scope
- Async conversion of the F05 tools, F06 nodes and LLM protocol (same behavior, same SQL).
- `cases` table + Alembic migration `0004`.
- SQS wrapper (`aioboto3`, LocalStack locally) with DLQ and redrive policy.
- FastAPI app: `/health`, `POST /alerts/{id}/investigations`, `GET /cases/{id}`, `GET /alerts/{id}/case`.
- Async worker (`python -m app.worker`), `AsyncPostgresSaver`, idempotent claim/complete.
- Dockerfile, `.dockerignore`, compose services `localstack`, `migrate`, `api`, `worker`.

## Out of scope
`interrupt()`/analyst decision (F08), long-term memory (F09), Redis (F10), auth (F11), LLM retries/fallback (F12), LangSmith (F13), Terraform/ECS (F17), transactional outbox.

## Interfaces
- `POST /alerts/{alert_id}/investigations` -> 202 `{case_id, alert_id, status}`; 404 if the alert does not exist; idempotent per alert.
- `GET /cases/{case_id}`, `GET /alerts/{alert_id}/case` -> `{case_id, alert_id, status, outcome, verdict, sar_draft, escalation_reason, error, attempts}`.
- SQS message body: `{"case_id": N, "alert_id": N}`.
- Graph thread id: `alert-{alert_id}`.

## Decisions
- At-least-once delivery. Idempotency comes from `cases.alert_id UNIQUE`, a status-guarded claim, and one idempotent completion `UPDATE`. Tools are read-only, so re-running a node after a crash is safe.
- Resume: if the thread has a checkpoint with pending nodes, call `ainvoke(None, config)`; otherwise start fresh.
- Checkpointer tables are created by `AsyncPostgresSaver.setup()` at worker startup, not by Alembic.
- DB commit then SQS send (no outbox): a failed send leaves a `queued` case; re-POST re-sends.

## Acceptance criteria
- POST twice for one alert -> one case; GET shows `completed` with a verdict.
- Worker killed mid-run and restarted resumes from the checkpoint without re-running finished nodes.
- Duplicate delivery of a completed case is acked and does nothing.
- A message that always fails is redelivered up to `maxReceiveCount`, lands in the DLQ, and the case is `failed`.
- `docker compose up` brings up postgres, localstack, migrate, api, worker; `/health` returns 200.
- ruff and mypy clean; F05/F06 test behavior preserved.

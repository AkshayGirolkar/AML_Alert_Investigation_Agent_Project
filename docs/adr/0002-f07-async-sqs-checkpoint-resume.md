# ADR 0002: Async execution, at-least-once SQS, checkpoint resume (F07)

## Status
Accepted (F07).

## Context
F06 ran one investigation synchronously from a CLI. F07 runs investigations as a service
(API -> SQS -> worker) that must survive worker crashes and duplicate deliveries, and
will later run on ECS Fargate.

## Decisions
- **Async end to end.** Tools use `psycopg.AsyncConnection`, nodes are `async def`, the LLM
  protocol is `async`, the worker uses `asyncio` + `aioboto3`. SQL and result shapes are
  unchanged. This deliberately touched F05/F06 code (and their tests).
- **At-least-once delivery, idempotent processing.** `cases.alert_id` is unique;
  `claim` only accepts queued/running cases; `complete` is one idempotent UPDATE; the
  message is deleted only after it. Tools are read-only, so re-running a node is safe.
- **Resume from checkpoints.** `AsyncPostgresSaver`, thread id `alert-{alert_id}`. A
  thread with pending nodes is continued with `ainvoke(None, config)`; a finished thread
  reuses its final state. Checkpoint tables are created by `saver.setup()` at worker start,
  not by Alembic.
- **DLQ via SQS redrive.** After `sqs_max_receive_count` receives SQS moves the message to
  the DLQ; the worker marks the case `failed` on the last receive. `POST` on a failed case
  requeues it.
- **Visibility heartbeat.** The worker extends visibility every half timeout while a run
  is in progress.
- **One image.** API and worker share a Docker image (command differs); config is env
  only, so ECS needs no code change (unset `AWS_ENDPOINT_URL`, use a task role).

## Consequences / known gaps
- Commit-then-send is not atomic: a failed send leaves a `queued` case; re-POST re-sends.
  A transactional outbox or sweeper is future work.
- A `running` case is only re-claimed when untouched for 0.75 x the visibility timeout
  (the worker heartbeat touches it every 0.5 x). This stops a duplicate message (for
  example a repeated POST while queued) from running the investigation twice; found in the
  first real end-to-end run. Redelivery after a crash therefore takes about one visibility
  timeout (5 min by default). Hard mutual exclusion across workers is still F10 (case locks).

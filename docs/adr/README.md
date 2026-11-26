# Architecture decision records

| ADR | Feature | Decision in one line | Status |
|---|---|---|---|
| [0001](0001-f06-sync-graph.md) | F06 | Plan-then-execute graph with a code-bounded tool loop; evidence kept as plain dicts so it checkpoints. (Its "synchronous, no checkpointer" part was replaced by 0002.) | Accepted, partly superseded by 0002 |
| [0002](0002-f07-async-sqs-checkpoint-resume.md) | F07 | API enqueues to SQS, worker runs the graph, at-least-once delivery made safe by idempotent side effects and checkpoint resume. | Accepted |
| [0003](0003-f09-long-term-memory.md) | F09 | Per-customer history in `PostgresStore`; context for prompts, never evidence; a prior approved escalation blocks auto-close. | Accepted |
| [0004](0004-f10-redis.md) | F10 | Redis for sanctions cache, shared token-bucket limiter, case locks, SSE; every use degrades if Redis is down. | Accepted |
| [0005](0005-separate-console-shared-db.md) | F10.5 | The analyst console is a separate project that shares only Postgres views and a request table (`docs/console-contract.md`). | Accepted |
| [0006](0006-f12-llm-gateway.md) | F12 | One gateway for timeout, retry, same-key model fallback, rate limit and a per-case cost budget. | Accepted |
| [0007](0007-f13-observability.md) | F13 | JSON logs, PII masking before LangSmith, in-process Prometheus-text metrics. | Accepted |
| [0008](0008-f15-evaluation-harness.md) | F15 | Frozen stratified eval set, metrics with Wilson intervals, escalate-all baseline, replayable gate. | Accepted |
| [0009](0009-f16-load-test.md) | F16 | Load test with a fake LLM and everything else real; two bottlenecks found and fixed; failure drills. | Accepted |
| [0010](0010-f17-aws-terraform.md) | F17 | ECS Fargate, RDS, ElastiCache, SQS, one NAT, local Terraform state, budget alarm in its own stack. | Accepted; code complete, never applied |
| [0011](0011-f11-auth-rbac.md) | F11 | JWT (Cognito JWKS, or HS256 for dev), analyst and supervisor roles, supervisor needed for high-risk decisions. | Accepted |

Numbering follows the order the ADRs were written, not the feature order.

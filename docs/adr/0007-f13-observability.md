# ADR 0007 – F13 observability

## Status
Accepted

## Decisions
- **Stdlib JSON logging, no new dependency.** `LOG_FORMAT=json` gives one JSON object per line; `case_id`, `alert_id` and `request_id` come from `contextvars`, so they follow asyncio tasks without changing function signatures (same pattern as F12's `case_scope`).
- **Masking is a pure regex + known-key function (`pii.mask`), not Presidio.** It is deterministic, instant and testable without the spaCy model. Presidio stays a possible upgrade behind the same `mask()` signature. Known PII keys are masked whole; free text is scanned for email, IBAN, SSN, phone-like numbers, hex account ids and API keys. Evidence ids (`tool:hash`) and plain amounts are deliberately left intact. The cost is false negatives for names inside free text: the main defence is that the structured KYC/transaction fields are keyed.
- **Masking is installed on the LangSmith client itself** (`hide_inputs`, `hide_outputs`, `hide_metadata`), created before LangChain first asks for the shared client. If a client already exists unmasked, tracing is switched off rather than leaking. Tracing is off unless `LANGSMITH_TRACING=true` and a key exist.
- **One trace per case**: the graph config carries `run_name`, tags and metadata (`case_id`, `alert_id`, `thread_id`).
- **Metrics without a metrics stack.** A small in-process registry (counters, histograms) renders Prometheus text. Because the worker is a separate process, durable facts (cases by status, review backlog age, recorded LLM cost/tokens) and queue/DLQ depth are read at scrape time by the API's `/metrics`, not duplicated in memory. The worker can serve its own in-process series (case outcome and duration, LLM latency, tokens, cost) on `WORKER_METRICS_PORT`. F17 maps these to CloudWatch.
- **`/ready` vs `/health`.** `/health` is unchanged (liveness plus DB). `/ready` needs the DB; Redis or SQS trouble reports `degraded` but still 200, matching the rule that Redis is never required.
- **Observability never fails a case.** Scrape sources are individually try/except; metric recording is plain arithmetic.

## Consequences
- Worker metrics reset on restart (counters), as usual for Prometheus; DB-derived gauges do not.
- Node-level latency is not measured yet; case duration and LLM call latency are. LangSmith traces show node timing.
- Request-path metrics use the route template, so label cardinality stays bounded.

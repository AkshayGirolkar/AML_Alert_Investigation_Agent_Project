# F13 – Observability

## Goal
Make every investigation traceable and measurable without leaking PII: LangSmith
traces with masking, structured JSON logs with case correlation, and operational
metrics (queue depth, latency, cost).

## In scope
- **Structured logging** (`app/observability/logging.py`): JSON formatter (stdlib, no new dep), configured by `LOG_FORMAT=json|text` and `LOG_LEVEL`; every record carries `case_id`, `alert_id`, `request_id` via `contextvars` (set in `Processor.handle`, API middleware). Wired into API, worker, CLI.
- **PII masking** (`app/observability/pii.py`): deterministic masker applied to trace inputs/outputs before they leave the process. Start with regex + known-field masking (names, account numbers, emails, phone, IBAN/SSN-like patterns, KYC fields); Presidio is an optional upgrade behind a flag (heavy: spaCy model).
- **LangSmith tracing** (`app/observability/tracing.py`): tracing is off unless `LANGSMITH_TRACING=true` and a key exists. Uses the LangSmith client's `hide_inputs`/`hide_outputs` hooks to call the masker. Each case is one trace tagged with `case_id`, `alert_id`, `thread_id`, model names; graph nodes and LLM calls appear as child runs.
- **Metrics** (`app/observability/metrics.py`): in-process counters/histograms exposed at `GET /metrics` (Prometheus text format, no extra service): cases processed by outcome, node latency, LLM latency/tokens/cost (from the F12 recorder), SQS queue depth (polled), DLQ depth, human-review backlog (from DB). CloudWatch export is F17.
- Health: `/health` unchanged; add `/ready` checking DB (and Redis/SQS as degraded, not failed).
- Settings and `.env.example` updated; ADR `0007-f13-observability.md`; `F13-explained.txt`.

## Out of scope
CloudWatch alarms/dashboards and log shipping (F17), eval datasets/experiments (F15), auth (F11), a Grafana/Prometheus stack in compose.

## Interfaces
- `configure_logging()`, `bind_case(case_id, alert_id)` context manager.
- `mask(obj) -> obj` (pure, idempotent, deep over dict/list/str).
- `setup_tracing(settings)` returns whether tracing is active; never raises.
- Metrics module exposes `record_case(outcome)`, `observe_node(name, seconds)`, etc.

## Acceptance criteria
- Log lines are valid JSON with `case_id` when emitted inside a case; text mode still works.
- Masker removes seeded PII (names, emails, phones, account numbers, KYC fields) from nested payloads, is idempotent, and leaves evidence IDs and numeric amounts intact.
- With a fake LangSmith client, nothing unmasked reaches `hide_inputs`/`hide_outputs` output; with tracing disabled no network call is made and the app runs.
- `/metrics` returns the expected series; counters move after a processed case in a test.
- Tracing, metrics and logging failures never fail a case.
- ruff, mypy and pytest pass.

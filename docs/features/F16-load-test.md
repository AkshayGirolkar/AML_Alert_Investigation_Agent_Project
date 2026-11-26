# F16 – Load Test

## Goal
Push 1,000 alerts through the real queue path (API → SQS → worker → Postgres/Redis) and find the bottleneck that limits throughput, fix what is cheap to fix, and record the numbers (throughput, latency percentiles, error and redelivery counts, resource use) for the README. The test answers "how many investigations per minute does this system sustain, and what runs out first?"

## In scope
- **Fake LLM mode** (`app/agent/fake_llm.py`): a deterministic stand-in that implements the F06/F12 `LLM` protocol, returns a schema-valid plan and verdict (a mix of close and escalate, seeded by alert id, citing evidence ids that exist in state so grounding passes), and sleeps a configurable latency (`FAKE_LLM_LATENCY_MS`, optional jitter; optional failure rate `FAKE_LLM_ERROR_RATE` for the retry path). Selected with `LLM_PROVIDER=fake` (default `openai`); the worker, graph and gateway are otherwise unchanged, so the rate limiter, cost recorder, checkpointer, tools and DB writes still run. Needed because 1,000 real investigations cost money and the OpenAI rate limit would measure OpenAI, not this system. The fake bills synthetic token counts so `llm_calls` rows are still written.
- **Load generator** (`loadtest/`, uses `locust`, already in `requirements-dev.txt`):
  - `loadtest/locustfile.py`: an `HttpUser` that picks an alert id from the load set and calls `POST /alerts/{id}/investigations`, then (optional task) polls `GET /cases/{id}` until a terminal or `awaiting_review` status to measure end-to-end time. Fixed, seeded alert list, no random picks, so reruns are comparable.
  - `scripts/loadtest_seed.py`: builds the load set (1,000 distinct alert ids, deterministic order) from the DB, preferring alerts not yet investigated; `--reset` deletes prior load-test cases/checkpoints/`llm_calls` for those alerts so a run starts clean. Prints the target database, like the other scripts, and refuses to run against a database that is not local unless `--force`.
  - `scripts/loadtest_run.py`: one command that resets, enqueues N alerts at a chosen rate (`--rate`, `--users`, `--duration` or `--count 1000`), waits for the queue to drain, and writes `loadtest/results/<run_id>.json` and a Markdown summary. Also supports a "burst" profile (all 1,000 enqueued at once, measures the queue-drain rate) and a "steady" profile (constant arrivals, measures latency under sustained load).
- **Measurements** (`app/loadtest/metrics.py`, pure functions, unit-tested): per run, from the `cases`, `audit_log`/events and `llm_calls` tables plus SQS attributes:
  - throughput (cases completed per minute, overall and per steady-state window, excluding ramp-up and the drain tail),
  - end-to-end latency (enqueue → verdict) p50/p95/p99, queue wait (enqueue → worker start) and run time (worker start → verdict) separately,
  - failures: cases in `error`, messages in the DLQ, redeliveries (receive count > 1), cases with duplicate side effects (must be 0: F07 idempotency),
  - API latency for the enqueue endpoint p50/p95/p99 and error rate,
  - cost and token totals from `llm_calls` (fake mode: synthetic).
  All arithmetic in Python/SQL, never by an LLM.
- **Resource sampling** (`scripts/loadtest_run.py --sample`): every 5 s, record Postgres active/idle/waiting connections and lock waits (`pg_stat_activity`), SQS queue depth and in-flight (from the F13 scrape gauges or `get_queue_attributes`), Redis ops/sec and rate-limiter wait time (from F13 metrics), worker CPU/memory (`docker stats`), and the worker's in-flight count. Saved next to the results so a bottleneck can be argued from data.
- **Parameter sweep** (local; repeated on AWS in F17): runs at worker concurrency 1, 4, 8, 16 (`WORKER_CONCURRENCY`) and with 1 vs 2 worker containers (`docker compose up --scale worker=2`), at a fixed fake latency (e.g. 1.5 s per LLM call) so scaling behaviour is visible. The sweep output is a table: concurrency → throughput, p95, errors.
- **Bottleneck hunt and fixes**: expected suspects, to be confirmed by measurement and not assumed: Postgres connection pools (SQLAlchemy pool, `psycopg_pool` for the checkpointer/store) sized below worker concurrency; checkpoint write volume; per-case `audit_log`/events inserts; the F10 rate limiter limit (`LLM_RATE_LIMIT_PER_SECOND` caps throughput by design: record the ceiling it implies); SQS long-poll batch size and visibility timeout versus run time (redelivery of slow cases); tool queries that miss an index on the full dataset (use `EXPLAIN ANALYZE` on the slowest). Each fix is a small change in its own commit with a before/after number in the report. Anything larger than a config or index change becomes a follow-up, not part of F16.
- **Failure drills** (run once, results recorded, not part of the repeatable sweep): kill the worker mid-run and confirm the queue drains and cases resume from checkpoints (F07 criterion at scale); stop Redis mid-run and confirm everything degrades without errors (F10); fake LLM error rate 10% and confirm retries/fallback keep the case error rate near 0 and nothing lands in the DLQ except real poison messages.
- **Docs**: ADR `0009-f16-load-test.md` (method, findings, what was fixed, remaining ceiling), `docs/features/F16-explained.txt`, a short "Performance" section for F18, `.env.example` (`LLM_PROVIDER`, `FAKE_LLM_*`) and CLAUDE.md updated.

## Out of scope
Load testing the real OpenAI path beyond one small sanity run (cost; throughput there is bounded by OpenAI rate limits and is reported as a calculation from the limiter settings, not measured at 1,000), autoscaling policy and ECS/ALB sizing (F17, which uses these numbers), testing the console, the Postgres-view contract or `/events` SSE at scale, any prompt or eval-threshold change, Prometheus/Grafana stack (the F13 text endpoint and the sampler are enough).

## Interfaces
- `FakeLLM(latency_ms, jitter_ms, error_rate, seed)` implementing the existing `LLM` protocol; `build_llm(settings)` picks it when `llm_provider == "fake"`.
- `compute_run_metrics(rows, window) -> LoadMetrics` (pure): throughput, latency percentiles, failure counts.
- `percentile(values, q)` shared with F15 if it already exists in `app/eval/metrics.py`; import it, do not duplicate.
- Result file: `{run_id, git_sha, profile, params{concurrency, workers, fake_latency_ms, rate_limit}, throughput_per_min, latency{e2e,queue,run}{p50,p95,p99}, api{p50,p95,p99,error_rate}, failures{errors, dlq, redeliveries, duplicates}, samples[...]}`.

## Rules to keep (from the design)
- **Idempotency under load:** after every run, a check query asserts exactly one case, one decision/audit entry per alert and no duplicated `llm_calls` for a completed node; any duplicate fails the run.
- **No real money by default:** fake LLM is the default for `loadtest_run`; real OpenAI needs `--live` and a hard `--max-cost-usd`.
- **Production safety:** the loadtest scripts refuse a non-local `DATABASE_URL` without `--force`, and never touch rows outside the seeded alert ids.
- **Comparable runs:** fixed alert list, fixed seed, recorded config; a result without its config is invalid.
- Develop against the ~100k slice for the harness itself; the recorded numbers use the full dataset, because tool query cost depends on table size.
- Never let the LLM do arithmetic; all percentiles and rates are computed in code.

## Acceptance criteria
- `FakeLLM` unit tests: schema-valid plan and verdict, deterministic for a given alert id and seed, cited evidence ids pass `validate_grounding`, latency and error rate are honoured (with a patched sleep), and `llm_provider=fake` wires into `build_llm`.
- Metrics unit tests without DB or queue: percentiles on one value, empty run, steady-state window excludes ramp-up and tail, redelivery and DLQ counting, duplicate detection.
- `loadtest_seed` is deterministic (same DB gives the same 1,000 ids in the same order) and its `--reset` removes only load-test rows.
- Integration (needs `docker compose up -d --build`, skipped otherwise): a 50-alert burst with the fake LLM completes, queue drains, 0 errors, 0 duplicates, results file written.
- A full 1,000-alert burst run completes with 0 duplicates and 0 DLQ messages at the chosen default concurrency, and the report states throughput and p95 for it.
- The sweep table exists for concurrency 1/4/8/16 and 1 vs 2 workers, and the report names the first resource to saturate with the supporting sample series.
- At least one bottleneck found by measurement is fixed, with before/after throughput; if none is found, the report says what the ceiling is and why (e.g. the rate limiter).
- Failure drills done and recorded: worker kill (queue drains, cases resume), Redis down (no errors), 10% LLM errors (error rate near 0).
- ruff, mypy and pytest pass; `loadtest/results/` is gitignored except one committed example result for the README.

## Decisions (open questions resolved)
1. **Where it is measured:** local Docker Compose first (F16 is complete when the local numbers and fixes are done, hardware recorded), then the same scripts run against AWS once F17 exists. The AWS run is a short, scheduled task inside F17, not a blocker for F16: `loadtest_run.py` takes `--api-url` and `--force` for a remote target, and the sampler reads CloudWatch (queue depth, ECS CPU/memory, RDS connections) instead of `docker stats`. Cost control for the AWS run: a 1,000-alert burst with the fake LLM takes minutes, so stand the stack up, run the sweep once (concurrency 4 and 8, 1 vs 2 tasks), record the results, and `terraform destroy` the same day. No real OpenAI calls on AWS.
2. **Fake LLM fidelity:** fixed latency (default 1.5 s, jitter off). It is the simplest setting to reason about, free, and makes scaling curves readable. `FAKE_LLM_LATENCY_FILE` replay is dropped from scope; revisit only if results look unrealistically clean.
3. **Alert pool:** exclude the 100 eval-set alerts and any alert that already has a case, so load tests never touch eval data or long-term memory. The memory store stays off for load runs (same as eval), so runs do not contaminate each other.
4. **Success bar:** no pass/fail gate. After the first sweep, record the measured ceiling and the resource that caused it as a documented number in the ADR and README. A CI performance regression gate is a possible follow-up. The one hard pass/fail is correctness under load: 0 duplicates, 0 unexpected DLQ messages.

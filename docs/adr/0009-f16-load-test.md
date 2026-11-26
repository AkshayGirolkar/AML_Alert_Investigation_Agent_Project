# ADR 0009 – F16 load test

## Status
Accepted

## Method
- **Fake LLM, real everything else.** `LLM_PROVIDER=fake` swaps OpenAI for `SyntheticLLM` (fixed 1.5 s per call, seeded verdicts that cite real evidence ids) inside the F12 gateway. The API, SQS (LocalStack), worker, LangGraph graph, tools, Postgres, Redis, rate limiter, checkpointer and cost recording all run for real. 1,000 real investigations would cost money and measure OpenAI's limits, not this system.
- **Fixed alert list.** `loadtest/datasets/load-1000.json`: 1,000 alerts in `md5(seed:alert_id)` order, never an eval-set alert and never one with a case. `loadtest_seed --reset` deletes only that set's rows (cases, LLM usage, checkpoints, memory records).
- **One run = enqueue through `POST /alerts/{id}/investigations`, wait for the drain, read results back from the database.** Latency is read from `cases`/`llm_calls` (worker start is the first LLM call minus its latency, a close proxy). Throughput is a steady-state number: the first and last 10% of finishes are dropped as ramp-up and drain tail. Resources are sampled every 5 s (Postgres activity, SQS depth, Redis, `docker stats`).
- **Hard pass/fail** is correctness only: drained, 0 duplicate side effects, 0 dead-lettered messages. There is no throughput gate; the ceiling is recorded here instead.
- **Hardware:** one laptop, Docker Desktop VM with 8 CPUs and 4 GB RAM, x86_64 macOS, 5.08M-row transactions table, Postgres 17 with default settings (`shared_buffers` 128 MB). Numbers are for comparison between runs on this machine, not absolute capacity.

## What was found
1. **Alias scan in `sanctions_screen`.** First sweep: throughput peaked at concurrency 16 (208 cases/min) and fell at 32 and 64; the 1,000-alert burst did 40 cases/min and took about 25 minutes, with Postgres at ~1,000% CPU. Timing each tool on fresh alerts showed `sanctions_screen` at 3.96 s per call against 0.3 s for the next slowest. The alias half of the search ran `similarity()` over 230k unnested aliases with no index (`EXPLAIN ANALYZE`: 2,707 ms; the Redis sanctions cache hid it for repeated names, which also made the first sweep's "cold" runs look like a cache-warmth effect). Fix, migration 0010: trigger-maintained `sanctions_aliases` with a trigram GIN index, queried with `alias % q` inside the same `set_limit` window (9 ms; identical rows on 25 names and in the parametrised tests). `sanctions_screen` now 0.06 s per call.
2. **Worker DB pool.** The psycopg pool shared by the checkpointer and memory store had the default 4 connections. At concurrency 32 a 16-connection pool gave 260 -> 316 cases/min (+22%); at 64, 292 -> 311 (+7%). `WORKER_DB_POOL_SIZE` is now a setting, 0 = auto (half the concurrency, clamped 4..16; Postgres allows 100 connections).
3. **One worker process is a single core.** With the first two fixes the worker sits at ~100% CPU from concurrency 32; 32 and 64 land within a few percent (311-316/min). A second worker process raises it (374/min at 2 x 16, Postgres then at ~517% average CPU on the 8-CPU VM).
4. **The designed ceiling is the rate limiter.** Default 5 LLM calls/s with a measured 2.30 calls per case allows about 130 cases/min, below everything above. With real OpenAI that is what limits throughput, so the engineering numbers here only matter if the limit is raised.

## Results (fake LLM at 1.5 s per call, burst, steady-state cases/min)
Before the fixes (first pass, 1 worker): c1 15.8, c4 62.6, c8 124.8, c16 207.9, c32 180.4, c64 154.5, 2 x 16 175.7; full 1,000 burst 39.9.
After the alias index (pool 4): c16 212.9, c32 259.5, c64 291.7, 2 x 16 374.2; **full 1,000 burst 286.9 (215 s, 0 errors, 0 duplicates, 0 DLQ)**.
After the pool change: c32 315.7, c64 311.4.
Run time per case is flat at ~3.3 s up to concurrency 8 (about 2.3 LLM calls at 1.5 s plus ~0.1 s of tools), rising only under contention. Enqueue API p95 stayed 0.5-1.8 s for 50 concurrent requests, except 6.4 s in one pre-fix run when the Docker VM was saturated.
Sweep files: `loadtest/results/{sweep1,sweep2-before,after-alias-index}/` (untracked); one full 1,000 run is committed as `loadtest/results/example-full-1000.{json,md}`.

## Failure drills (200 alerts, concurrency 8, fake LLM)
- **10% LLM errors:** 514 LLM calls, 46 failed and were retried by the gateway; 200/200 cases finished, 0 failed, 0 DLQ, 0 duplicates, no redeliveries. Throughput 110 cases/min against ~120 clean.
- **Redis stopped for 20 s at 138 of 200 unfinished:** 200/200 finished, 0 errors, 0 DLQ, 0 duplicates, throughput unaffected (the rate limiter fails open, sanctions cache and locks degrade).
- **Worker SIGKILLed at 135 of 200 unfinished (8 cases in flight), restarted after 5.5 s:** the 8 messages reappeared after the SQS visibility timeout, all resumed from their checkpoints and finished; 200/200 done, 0 errors, 0 DLQ, 0 duplicates, and the total LLM call count (468) equals a clean run, so no completed work was repeated. The run took 340 s instead of ~110 s because those 8 waited for the 300 s visibility timeout.
- The first attempt at the redis and kill drills fired before the run began (old finished cases looked like instant progress) and then the runner refused a non-empty queue; both were fixed (reset and purge in the driver, an assertion that the fault lands mid-run) and rerun. Only the rerun results are quoted.

## Consequences
- Larger fixes are follow-ups, not done here: a second worker process per container (or more tasks in F17), `shared_buffers`/`work_mem` tuning (Postgres runs on defaults), faster `peer_comparison` (0.3-0.4 s, now the slowest tool), and a shorter visibility timeout or faster dead-worker detection if recovery time matters.
- F17 should rerun the sweep on AWS (fake LLM, short, `terraform destroy` the same day) using `loadtest_run --api-url`; autoscaling thresholds can use the queue-depth and per-worker numbers above.
- `loadtest_run` refuses a worker that is not on the fake LLM unless `--live --max-cost-usd` is given, and refuses non-local targets without `--force`.
- Migration 0010 means any code that bulk-loads `sanctions_entities` keeps the alias table correct through triggers; direct writes to `sanctions_aliases` are not supported.
- The locust file works with the same manifest but the repeatable numbers come from `loadtest_run` (httpx), which also resets, drains and writes the result.

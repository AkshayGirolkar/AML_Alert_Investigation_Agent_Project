# Load run 20261126T102016-burst-1000

- Profile: **burst**, outcome: **drained**, git `0f4ecf7`
- Cases: 1000/1000 finished in 215.2 s
- Throughput: **278.9** cases/min overall, **286.9** steady state
- Correctness: errors 0, unfinished 0, redeliveries 0, duplicates **0**, DLQ **0**
- LLM: cost $0.9733, tokens 5853377 in / 158745 out

| Latency | p50 | p95 | p99 | n | unit |
|---|---|---|---|---|---|
| end to end | 107.0 | 194.1 | 199.7 | 1000 | s |
| queue wait | 101.6 | 188.9 | 195.3 | 1000 | s |
| run time | 5.3 | 7.4 | 9.7 | 1000 | s |
| enqueue API | 729.8 | 939.7 | 1008.9 | 1000 | ms |

Enqueue API: 1000 requests, 0 errors.

## Config

- `profile` = burst
- `count` = 1000
- `rate` = None
- `enqueue_concurrency` = 50
- `manifest` = loadtest/datasets/load-1000.json
- `trim` = 0.1
- `hardware` = x86_64 Darwin
- `note` = sweep full-c32
- `label` = full-c32
- `workers` = 1
- `LLM_PROVIDER` = fake
- `FAKE_LLM_LATENCY_MS` = 1500
- `FAKE_LLM_JITTER_MS` = 0
- `FAKE_LLM_ERROR_RATE` = 0.0
- `FAKE_LLM_ESCALATE_PCT` = 30
- `WORKER_CONCURRENCY` = 32
- `LLM_RATE_LIMIT_PER_SECOND` = 1000
- `LLM_RATE_LIMIT_BURST` = 1000

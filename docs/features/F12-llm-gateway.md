# F12 – LLM Gateway

## Goal
Make LLM calls resilient and measurable: timeouts, retries with backoff, provider
fallback (OpenAI -> Anthropic), and per-case token and cost tracking. The graph keeps
using the same one-method `LLM` interface.

## In scope
- `app/agent/gateway.py`: `GatewayLLM` implementing `LLM.structured`, composed of
  - per-call timeout (`llm_timeout_seconds`),
  - retry with exponential backoff + jitter on transient errors only (timeouts, 429, 5xx, connection),
  - fallback to a secondary provider after the primary exhausts retries, or immediately on a hard outage.
- Fallback uses the same `OPENAI_API_KEY`: a secondary OpenAI model (`LLM_FALLBACK_PLAN_MODEL`, `LLM_FALLBACK_ASSESS_MODEL`, default gpt-4o-mini). No new key or dependency needed.
- The `Provider` protocol (`structured_with_usage`) lets an Anthropic/Bedrock provider be added later without touching the gateway.
- Usage capture: input/output tokens, model, provider, latency, fallback-used per call.
- Cost: `app/agent/pricing.py` price table (USD per 1M tokens) -> cost computed in Python, never by the LLM.
- Persistence: migration adds `llm_calls` (case_id, node, provider, model, tokens in/out, cost_usd, latency_ms, attempt, fallback, error) and a per-case total view/query; written idempotently (case_id, call_seq).
- Settings and `.env.example`: `LLM_FALLBACK_*_MODEL`, timeout, max retries, optional per-case cost budget.
- Wire into `app/worker/main.py` and `app/agent/run.py` (order: limiter -> gateway).

## Out of scope
Anthropic/Bedrock providers (interface allows them later), streaming, LangSmith tracing and PII masking (F13), eval cost comparison dashboards (F15), auth (F11).

## Interfaces
- `LLM.structured(schema, messages) -> T` unchanged; `GatewayLLM(primary, fallback, policy, recorder)`.
- `UsageRecorder.record(case_id, node, usage)`; no-op when no DB (CLI demo prints totals).
- Errors: `LLMUnavailable` after all providers fail; the worker treats it as a retryable failure (SQS redelivery / DLQ), not a verdict.
- Budget exceeded -> `CostBudgetExceeded`; the nodes catch it, the case gets no verdict and goes to human review (reason "no verdict").

## Acceptance criteria
- Transient error on primary is retried with backoff, then succeeds; no fallback used.
- Persistent primary failure falls back to secondary and records `fallback=true`.
- Non-transient errors (auth, schema validation) are not retried; validation failure still follows the graph's existing retry.
- A call exceeding the timeout is cancelled and counted as a transient failure.
- Per-call and per-case tokens/cost are stored and match the price table. A re-delivered case continues `call_seq` after its recorded calls (real spend is counted, a duplicate insert of the same call is ignored).
- Rate limiter token is consumed once per attempted provider call.
- Everything works with fakes in unit tests (no network); `live` marker tests cover real OpenAI.
- ruff, mypy and pytest pass.

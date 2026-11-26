# ADR 0006 – F12 LLM gateway

## Status
Accepted

## Decisions
- **Same `LLM` interface, new wrapper.** `GatewayLLM` implements `structured()`, so graph nodes keep their signatures. Providers expose `structured_with_usage()` so token counts reach the gateway.
- **Fallback is a second OpenAI model on the same key** (gpt-4o -> gpt-4o-mini), because no second provider key exists. This covers model-level outages and rate limits, not a full OpenAI outage. The `Provider` protocol allows Anthropic/Bedrock later.
- **Retry only transient errors** (timeouts, connection, 408/409/429/5xx), classified by shape so any provider works. The OpenAI client's own retries are off (`max_retries=0`) so the gateway is the only retry layer. Auth, bad-request and schema-validation errors fail immediately.
- **Rate limiter inside the gateway**, one token per real provider call (retries and fallbacks included). `RateLimitedLLM` stays for tests but is no longer wired.
- **Case attribution by context variable** (`case_scope`, set in `Processor.handle` and the CLI), not by changing node signatures. No scope -> no recording and no budget.
- **Cost in Python from a price table** (`pricing.py`); unknown models cost 0 and warn. Prices are a snapshot and must be checked.
- **`llm_calls` is append-only, unique on (case_id, call_seq).** A redelivered case reloads spend and the next sequence number from the table, so real repeat spend is counted and a duplicate insert is ignored. Recording failures are logged, never raised.
- **Budget exhaustion is not a failure.** Nodes catch `CostBudgetExceeded`: no verdict, no re-assess, deterministic SAR draft, human review. The budget is checked before each call, so a case can overshoot by one call.

## Consequences
- Failed attempts are recorded (0 tokens, error text), so retry and fallback rates are queryable.
- The budget is per case across redeliveries but not per customer or per day.

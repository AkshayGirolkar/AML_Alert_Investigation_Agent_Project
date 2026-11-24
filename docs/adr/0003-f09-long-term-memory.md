# ADR 0003 – F09 long-term memory

## Status
Accepted.

## Decisions
- **LangGraph `AsyncPostgresStore`, plain key-value.** Ships with `langgraph-checkpoint-postgres`, shares the worker's connection pool, tables come from `store.setup()` (like the checkpointer, ADR 0002). No embeddings in v1: the question the agent needs answered is "what happened last time for this customer", which is a namespace lookup, not a similarity search.
- **Namespace per customer, key per alert.** `("customer_history", entity_id)` / `alert-{id}`. Overwriting on re-run keeps `write_memory` idempotent under at-least-once SQS delivery.
- **Store is a `Nodes` dependency**, not LangGraph's injected `store` argument, which is awkward on bound methods and makes the unit tests harder. Same pattern as the other dependencies.
- **History is context, not evidence.** It carries no evidence IDs, so `check_grounding` is untouched and memory cannot make an ungrounded verdict look grounded. The prompt says so.
- **Hard rule outside the LLM.** A prior analyst-approved escalation blocks auto-close in `routing.py`, next to `no_auto_close_rules`. A model should not be trusted to remember to be careful.
- **Failure policy.** Recall failure degrades to no history (an investigation without memory is still valid). Write failure raises, so the message is retried, because silently losing analyst feedback is worse than a retry.
- **No ground truth in memory.** Only the agent's verdict and the analyst's decision are stored; otherwise evals would leak labels into later prompts.

## Consequences
- Concurrent alerts on one customer do not see each other until one finishes (read at start, write at end). Accepted; a per-customer lock belongs to F10 (case locks).
- An escalated alert awaiting review is not in memory until the analyst decides.
- Memory grows without bound; TTL is deferred.

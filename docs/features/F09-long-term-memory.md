# F09 – Long-term memory (PostgresStore)

## Goal
A second alert on the same customer sees the prior outcome and analyst feedback.

## In scope
- `recall_history` node (after `load_kyc`) and `write_memory` node (last step of both the auto-close and the analyst-review paths).
- `AsyncPostgresStore` in the worker (tables created by `store.setup()`, no Alembic migration, same as the checkpointer). `InMemoryStore` in tests; optional Postgres store in the CLI (`--memory-pg`).
- Namespace `("customer_history", <customer_key>)`, key `alert-{alert_id}` (rewrite = overwrite, so SQS redelivery and crash resume are idempotent).
- Customer key: `kyc.entity_id`, else `account:{account_id}`.
- Recalled history is injected into the plan and assess prompts as context, never as evidence.
- Deterministic rule: a customer with a prior analyst-approved escalation is never auto-closed (setting `memory_block_auto_close_on_prior_sar`).

## Out of scope
Semantic / pgvector "similar past cases" retrieval, memory expiry (TTL), analyst UI for memory, review of high-risk closures, cross-customer memory.

## Interfaces
- State: `history: list[dict]` (newest first, at most `memory_history_limit`).
- Record: `{alert_id, rule_id, outcome, disposition, typology, risk_score, confidence, decision, analyst_id, analyst_notes, recorded_at}`. `decision`, `analyst_id`, `analyst_notes` are null for auto-closed alerts. Ground truth (`is_true_positive`, IBM `typology`) is never stored.
- `build_graph(..., store=None)`; with no store both memory nodes are no-ops.
- Failure policy: recall degrades to empty history on store errors; write errors propagate (SQS retries).

## Acceptance criteria
- After alert A on customer X is decided, alert B on X gets A's outcome and analyst notes in its prompts.
- A prior approved escalation prevents auto-close of a later alert on that customer; a prior rejection does not.
- Another customer's history is never recalled.
- Writing memory twice for the same alert leaves one record.
- History does not satisfy grounding: citing it is rejected like any unknown evidence ID.
- No store configured: F06–F08 behaviour and tests are unchanged.
- ruff, mypy and pytest pass.

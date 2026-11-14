# F05 – Agent Tools

## Goal
Five investigation tools the F06 graph can call: `query_transactions`, `counterparty_network`, `sanctions_screen`, `adverse_media`, `peer_comparison`. Each is a tested function over Postgres that returns an `Evidence` record with a deterministic ID, so F06's `validate_grounding` can check every cited ID. LangChain wrappers expose them to the model.

## In scope
- `app/agent/evidence.py`: `Evidence` model, `make_evidence_id`.
- `app/agent/tools/`: one pure function per tool (`transactions.py`, `network.py`, `sanctions.py`, `media.py`, `peers.py`), validated argument models (`schemas.py`), and `registry.py` (`build_tools`).
- `langchain-core` added to `requirements.txt`.
- Unit tests (no DB) and integration tests (Docker Postgres, `aml_test`).

## Out of scope
The graph, tool loop and its 8-call cap (F06), Redis sanctions cache (F10), LLM retries/fallback (F12), SQS/API, new tables or migrations. A `Settings` change is not needed.

## Interfaces
- Pure functions: `fn(conn: psycopg.Connection, **args) -> Evidence`. Sync, matching F02–F04. Arguments are validated by a pydantic model; invalid input returns an `Evidence` with `status="error"` and no ID instead of raising. Database/infrastructure errors propagate (the caller retries; an outage must never become evidence).
- `Evidence`: `evidence_id | None`, `tool`, `status` (`ok` | `empty` | `error`), `summary` (a one-line sentence built in Python), `data` (JSON-safe dict), `args`.
- `evidence_id = "<tool>:<first 10 hex of sha256(canonical json of args)>"`. Same call, same ID, so repeats are idempotent and de-duplicate in state. Error results have no ID, so they cannot be cited.
- `build_tools(conn_factory, embedder=None) -> list[StructuredTool]`: each tool takes the alert's `account_id` (never a free-text name the model could mistype) and returns `Evidence.model_dump(mode="json")`.

## Decisions
- **Ground truth never reaches the agent.** No tool selects `is_laundering`, `is_decoy`, `theme`, `planted_matches`, or alert labels. Tests assert this.
- **Arithmetic is SQL/Python.** Sums, counts, percentiles and ratios come from SQL; amounts are strings (`Decimal`), grouped per currency because there is no FX (the same limitation as F04).
- **Bounded output.** Every tool caps rows (limits are validated: samples <= 50, hops <= 2, neighbours per node <= 15 (default 5), peers sampled deterministically by `md5(entity_id)`), so results fit in the LLM context.
- **`counterparty_network`**: recursive CTE, 2 hops max, undirected, each node expands to its top-K counterparties by transaction count (`LATERAL ... LIMIT K`) so a hub cannot explode the walk. Reports `root_degree` and `truncated`.
- **`sanctions_screen`**: resolves `account_id` -> entity -> KYC name and country, then pg_trgm similarity against `name` (GIN index) and `aliases`. The session trigram limit is restored after the call. Threshold defaults to 0.5, the F04 value.
- **`adverse_media`**: articles for the account's entity. Without a `query`, newest first (no embedder needed). With a `query`, ordered by pgvector cosine distance, using the injected embedder (`OpenAIEmbedder` in production, `FakeEmbedder` in tests). Snippets only, not full bodies.
- **`peer_comparison`**: peers = other entities with the same `entity_type` and country. Compares outgoing transaction count, distinct counterparties and outgoing total, each as a percentile rank, plus the ratio of the window's outgoing total to `expected_monthly_volume`. Amount metrics use all currencies summed the way F03 derived `expected_monthly_volume`; this is noted in the output.
- **Unknown account** returns `status="empty"` with `account_exists: false`, not an error.

- **Measured on the full dataset (5.08M transactions, busiest alert account, 169k txns), 2026-11-14:** `query_transactions` 0.46 s, `counterparty_network` 0.18 s, `peer_comparison` 1.1 s, `adverse_media` 0.01 s, `sanctions_screen` 2.4-5 s. The sanctions time is the alias scan (`unnest(aliases)` has no index). Known limitation: a GIN trigram index on aliases needs a migration, which is out of scope here; the F10 Redis cache absorbs repeats. Decide in F10 whether to add the index.

## Tests
- Unit: evidence ID determinism and stability, argument validation (bad ids, reversed window, hops 3, oversized limit), registry (five tools, names, schemas, errors returned as data, no ID on error).
- Integration, per tool, hand-built data: correct aggregates and per-currency totals; sample cap; empty and unknown account; 2-hop reach and hop labels; hub cap and `truncated`; exact, reordered and typo sanctions matches and a non-match; the trigram limit restored; media ordering by query with `FakeEmbedder` and without a query; percentile and volume ratio; no ground-truth keys anywhere in any result.

## Acceptance criteria
- Every tool has unit and integration tests and handles empty, unknown and malformed input.
- Same arguments give the same `evidence_id`; error results carry none.
- No result exposes ground-truth fields.
- `ruff check .`, `mypy .` and `pytest` pass; `requirements.txt` updated; nothing outside F05 scope changes.

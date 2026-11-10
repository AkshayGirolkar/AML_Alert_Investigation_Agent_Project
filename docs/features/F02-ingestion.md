# F02 – Transaction Ingestion (IBM HI-Small)

## Goal
Load the three HI-Small raw files into Postgres as normalized tables, with ground-truth labels and laundering typologies linked to transactions.

## Source data (measured)
| File | Rows | Notes |
|---|---|---|
| `data/raw/HI-Small_Trans.csv` | 5,078,345 | No transaction ID. Minute-level timestamps, 2022-09-01 to 2022-09-18. 5,177 rows have `Is Laundering=1`. |
| `data/raw/HI-Small_accounts.csv` | 518,581 | 166,207 distinct entities. |
| `data/raw/HI-Small_Patterns.txt` | 370 attempts, 3,209 txn lines | `BEGIN LAUNDERING ATTEMPT - <TYPE>: <detail>` ... `END`. |

Facts the schema relies on:
- Account number alone is not unique (8 collisions). Key accounts by `(bank_id, account_number)`.
- Transactions use zero-padded bank codes. Normalize with `int(bank)` to join to accounts (100% match).
- Bank names are not unique, so key banks by ID.
- All 3,209 pattern lines match a labeled transaction exactly. 1,968 of the 5,177 laundering rows belong to no listed attempt.
- Typologies: FAN-OUT, FAN-IN, CYCLE, GATHER-SCATTER, SCATTER-GATHER, BIPARTITE, STACK, RANDOM.

## In scope
Tables: `banks`, `entities`, `accounts`, `transactions`, `laundering_attempts`, `laundering_attempt_transactions`.

- `banks` (`bank_id` int PK, `bank_name`)
- `entities` (`entity_id` text PK, `entity_name`, `entity_type`)
- `accounts` (`account_id` serial PK, `bank_id` FK, `account_number`, `entity_id` FK, UNIQUE(`bank_id`, `account_number`))
- `transactions` (`txn_id` bigint PK = CSV line order, `ts`, `from_account_id` FK, `to_account_id` FK, `amount_received`, `receiving_currency`, `amount_paid`, `payment_currency`, `payment_format`, `is_laundering`)
  - Amounts are NUMERIC, never float.
  - Indexes: `(from_account_id, ts)`, `(to_account_id, ts)`, partial index on `is_laundering`.
- `laundering_attempts` (`attempt_id` PK, `typology`, `detail_raw`, `max_degree_or_hops`)
- `laundering_attempt_transactions` (`attempt_id` FK, `txn_id` FK), matched on all 11 fields.

Loader: `COPY` into UNLOGGED all-TEXT staging tables, then `INSERT ... SELECT` with normalization, then drop staging. Re-runnable (idempotent).
Dev slice: keep all laundering rows and the accounts they touch, then add random normal rows.

## Out of scope
- `kyc_profiles` (F03, keyed by `entity_id`), sanctions, adverse media, alerts and cases.
- Partitioning. The data spans 18 days, so monthly partitions are pointless. Revisit as a separate `EXPLAIN ANALYZE` exercise.
- Rules engine (F04).

## Depends on / current state (already done, do not redo)
- Postgres: `docker compose up -d` starts `aml-postgres` (pgvector/pgvector:pg17, pgvector enabled). Host port **5433** (5432 is Postgres.app). Database/user `aml`; password in `.env` (never print or commit it).
- Python 3.12 env: `.venv` (use `.venv/bin/python`). Runtime deps in `requirements.txt` (sqlalchemy, alembic, asyncpg, psycopg[binary], pydantic-settings, pgvector); dev tools in `requirements-dev.txt`. `my_venv` (3.14) is obsolete.
- Settings: `app/config.py` -> `get_settings().sync_database_url` (psycopg) / `.async_database_url` (asyncpg).
- Schema: `app/db/models.py` and Alembic migration `alembic/versions/0001_create_ingestion_tables.py` (tested up/down/up). Apply with `.venv/bin/alembic upgrade head`. The tables are empty.
- Not yet present: `scripts/`, `tests/`, Redis, LocalStack, CI.

## Parsing and loading rules (decisions)
- **Entry point:** `scripts/load_hi_small.py` with `--slice` (dev slice) or `--full`; reads `data/raw/`. Uses psycopg `COPY` (sync).
- **Idempotent:** each run truncates the six tables (and `RESTART IDENTITY` for accounts), then reloads, so a second run gives identical counts. Do not use row-by-row upserts.
- **Index handling:** drop the three secondary `transactions` indexes before loading and recreate them after (they are in the migration, so recreate with the same names/definitions). Run `ANALYZE` at the end.
- **Order:** banks -> entities -> accounts -> transactions -> laundering_attempts -> laundering_attempt_transactions.
- **`txn_id`:** 1-based data-row number in `HI-Small_Trans.csv` (header excluded). In slice mode keep the original `txn_id` values (do not renumber).
- **Timestamps:** source format `YYYY/MM/DD HH:MM`, no timezone, stored as `timestamp`.
- **Bank IDs:** `int(value)` for both accounts and transactions (transactions are zero-padded, e.g. `010`, `00220`). Join to accounts on `(int(bank), account_number)`; all 515,088 account pairs used in transactions match.
- **Entities:** `entity_type` = text before ` #` in `Entity Name` (types: Partnership, Corporation, Sole Proprietorship, Country, Individual, Direct). All 518,581 names contain ` #`. Each entity has exactly one type.
- **Banks:** derive `(bank_id, bank_name)` from the accounts file, keyed by `bank_id` only (names are not unique).
- **Currencies / payment_format:** keep the source text as is (e.g. `US Dollar`, `Cheque`).
- **Duplicates:** 9 transaction rows are exact duplicates of another row. This is fine because `txn_id` is the key. None are among the laundering rows.
- **`attempt_id`:** 1-based order of the `BEGIN` blocks in the patterns file (370).
- **Typology parsing:** text after `ATTEMPT - `, before the first `:`. Header shapes in the file: `CYCLE:  Max N hops` (54), `GATHER-SCATTER:  Max N-degree Fan-In` (51), `BIPARTITE` (49), `FAN-OUT:  Max N-degree Fan-Out` (48), `SCATTER-GATHER` (44), `STACK` (43), `RANDOM:  Max N hops` (41), `FAN-IN:  Max N-degree Fan-In` (40). `max_degree_or_hops` = the N, or NULL for BIPARTITE, SCATTER-GATHER and STACK. `detail_raw` = the full header text.
- **Linking attempts to transactions:** each line inside a block has the same 11 fields as the CSV. Match it on all 11 fields against rows where `is_laundering` is true (this is unambiguous: no duplicates among them). Fail loudly if any line is unmatched or matches more than one row. Expected: 3,209 links. The other 1,968 laundering rows legitimately belong to no attempt.
- **Dev slice (`--slice`):** fixed seed (default 42). Keep all 5,177 laundering rows, every pattern transaction and all accounts they touch, then add random non-laundering rows until about 100,000 transactions. Load every account row that appears in a kept transaction, plus its entities and banks. Print the counts it loaded.
- **Performance target:** full load in a few minutes on a laptop. Do not use per-row INSERTs.

## Tests
- `tests/` with pytest. Parsing helpers (header parser, bank-ID normalizer, entity-type splitter) get unit tests using tiny inline samples, not the real files.
- One integration test against the Docker Postgres that loads the slice and checks the acceptance criteria that apply to a slice (no NULL account IDs, every pattern link resolves, loader is repeatable).
- The full-file counts below are checked manually after the full load.

## Acceptance criteria
- Row counts after load: transactions 5,078,345; accounts 518,581; entities 166,207; laundering_attempts 370; laundering_attempt_transactions 3,209.
- No transaction has a NULL `from_account_id` or `to_account_id`.
- `count(*) WHERE is_laundering` = 5,177.
- A FAN-OUT attempt with "Max 16-degree" has 16 transactions from one source account.
- Running the loader twice leaves the same counts.
- `EXPLAIN ANALYZE` on "all transactions for an account in a date range" uses an index scan.
- The loader reads files as untrusted data: no code is executed from `data/`.

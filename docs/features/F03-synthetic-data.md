# F03 – Synthetic KYC, Sanctions, and Adverse Media

## Goal
Generate seeded, reproducible KYC profiles for every loaded entity, load the OpenSanctions sanctions list with a few deliberately planted name matches, and create template-written adverse-media articles (no LLM for the text) embedded with OpenAI into pgvector. This gives the F05 tools (`sanctions_screen`, `adverse_media`, `peer_comparison`) real data to query.

## Source data
| Source | Notes |
|---|---|
| `entities` table (from F02) | 166,207 entities in a full load; fewer in the dev slice. `entity_name` is a placeholder such as `Corporation #12345` (text before ` #` is the type), so real-looking names must be generated. |
| `transactions` / `accounts` (from F02) | Source of real activity for `expected_monthly_volume` and of `is_laundering` for media selection. |
| OpenSanctions consolidated sanctions, flat CSV (`targets.simple.csv`) | Downloaded 2026-11-10 from `https://data.opensanctions.org/datasets/latest/sanctions/targets.simple.csv` to `data/opensanctions/` (gitignored, 78.8 MB). Free for non-commercial use. **Measured:** 73,922 rows, all `id` values distinct, no empty names. Columns: `id, schema, name, aliases, birth_date, countries, addresses, identifiers, sanctions, phones, emails, program_ids, dataset, first_seen, last_seen, last_change`. `aliases`, `countries` and `dataset` are `;`-separated lists. `schema` counts: Person 41,179, Organization 17,762, LegalEntity 5,066, CryptoWallet 2,593, Company 2,531, Security 2,363, Vessel 2,031, Airplane 342 (plus smaller types). The file changes daily, so counts will drift on re-download. |
| `app/synth/media_templates.json` (already written) | 23 themes (17 laundering-related, 6 decoy). Each theme has headlines, openings, details and closings. An article is one headline + one opening + two details + one closing, picked by a seeded RNG, with placeholders filled in Python. About 5,700 distinct laundering-related and 1,500 decoy combinations. All people, companies, outlets and agencies are fictional. |
| OpenAI API | **Embeddings only:** `text-embedding-3-small` (1536 dims). Key in `.env` as `OPENAI_API_KEY`. No LLM writes text in F03. |

PEP data is out of v1.

## In scope
New Alembic migration `0002` (tested up/down/up like `0001`). It creates the extensions `vector` and `pg_trgm` (`CREATE EXTENSION IF NOT EXISTS`) and these tables:

- `kyc_profiles` (`entity_id` text PK/FK to `entities`, `full_name`, `occupation`, `country`, `expected_monthly_volume` NUMERIC, `risk_rating` text in low/medium/high)
- `sanctions_entities` (`source_id` text PK = CSV `id`, `name`, `aliases`, `schema_type` = CSV `schema`, `countries`, `dataset`, `first_seen`)
  - Keep `aliases` and `countries` as the source `;`-separated text, or text arrays. Decide in the plan.
  - Load only rows with `schema` in Person, Organization, LegalEntity, Company. Skip CryptoWallet, Security, Vessel, Airplane and the rest, since they can't be customers.
  - GIN `gin_trgm_ops` index on `name`.
- `planted_matches` (`entity_id` FK, `sanctions_source_id` FK, `variant_kind` in exact/reordered/typo). This is the answer key for F15.
- `adverse_media` (`media_id` serial PK, `entity_id` FK, `headline`, `body`, `published_at`, `is_decoy` bool, `theme` text = template theme id, `embedding` vector(1536))
  - HNSW index on `embedding` with cosine ops.

Also add the extensions to `docker/postgres/init/01-extensions.sql` (`pg_trgm` is missing there today). The migration owns them, so tests do not depend on the init script.

Scripts, with logic in `app/synth/`:
- `scripts/generate_kyc.py`
- `scripts/load_sanctions.py`
- `scripts/plant_sanctions_matches.py`
- `scripts/generate_media.py`

Config in `app/config.py`: add `seed` (default 42), `openai_api_key`, `data_dir` (default `data`). Update `.env.example`. Add `faker` and `openai` to `requirements.txt`.

## Out of scope
- The agent tools themselves (F05) and the rules engine (F04).
- Redis sanctions cache (F10). LLM provider fallback, retries and cost tracking (F12).
- PEP lists.
- Alerts and cases.

## Depends on / current state
- F02 loaded: `entities`, `accounts`, `transactions` with `is_laundering`. Run `.venv/bin/python -m scripts.load_hi_small --slice` first.
- Postgres on host port 5433 (pgvector image). `.venv` is Python 3.12.
- `app/ingest/loader.py` has reusable helpers (`_conninfo`, `_copy_file`, `_log`). They are underscore-private today. Promote them to a shared module (for example `app/db/copy.py`) and import from both, without changing F02 behaviour.
- No LLM, embedding or Faker code exists yet.

## Decisions
- **Scope of KYC:** one profile per entity of type Individual, Corporation, Partnership or Sole Proprietorship. Skip `Country` and `Direct` entities, which are not customers. Confirm this when the real counts are known.
- **Determinism:** per-entity values come from `Faker.seed_instance(int(md5(seed || entity_id)[:8], 16))` plus a local `random.Random` seeded the same way. This matches the F02 slice hashing. Results do not depend on row order.
- **Names:** Individuals get a person name, the others a company name. These replace the placeholder `entity_name` in the KYC view. `entities` is not modified.
- **Volume:** `expected_monthly_volume` = the entity's real average monthly outgoing amount, computed in SQL, times a seeded multiplier (for example 0.5 to 2.0). The LLM never computes numbers.
- **Risk rating:** a seeded rule on country risk and entity type, not an LLM call.
- **Sanctions load:** download once to `data/opensanctions/`, `COPY` into an UNLOGGED all-TEXT staging table, then `INSERT ... SELECT` into `sanctions_entities`, drop staging. Truncate-and-reload, so it is idempotent. Treat the file as untrusted data.
- **Planted matches:** about 15 entities chosen by seed. Each takes a real sanctions name as an exact copy, a word-reordered copy, or a one-character-typo copy. The planter updates `kyc_profiles.full_name` and writes `planted_matches`. Re-running restores non-planted names first (regenerate then plant), so it is idempotent. Only `Individual` and `Corporation` entities are eligible. Pair them by type: `Individual` with `Person`, `Corporation` with `Company`, `Organization` or `LegalEntity`.
- **Adverse media selection:** about 2% of KYC entities, by seed. Oversample true launderers (entities with an account in a transaction where `is_laundering`) but also include non-launderers as decoys (`is_decoy = true`). Some launderers get no media. The exact ratios are constants in code and are printed on each run.
- **Article generation (templates, no LLM):** `app/synth/media_templates.json` (version field `version`). For each selected entity, pick a theme whose `category` matches the selection (`laundering` for true-positive media, `decoy` for decoys) and whose `applies_to` matches the entity (`person` for Individuals, `org` for the other types). Then pick one headline, one opening, two distinct details and one closing, all from a `random.Random` seeded per entity. Fill the placeholders: `{name}` from the KYC `full_name`, other names and cities from the same per-entity Faker, `{amount}` and `{amount2}` formatted by Python (never by an LLM), `{outlet}`, `{authority}`, `{court}`, `{industry}` from the file's `fillers`. `published_at` is a seeded date. Decoy themes are adverse but not about laundering (tax dispute settled, labor dispute, product recall, allegations cleared, data breach fine, bankruptcy, libel case). Articles never name real people or agencies.
- **Embeddings (OpenAI, the only API use):** embed `headline + "\n" + body` with `text-embedding-3-small` in batches. Cache embeddings as JSONL under `data/media_cache/embeddings.jsonl`, keyed by `sha256(text) + model`. A re-run, or a DB reset, reads the cache and makes no API calls. Changing the seed or template text only re-embeds the changed articles.
- **Cost guard:** embeddings for about 3,300 articles should cost a few cents. `--dry-run` prints the number of texts to embed, uncached count and estimated cost, then exits. `--limit N` caps API calls. Never log the API key. Check current OpenAI pricing before the first real run.
- **Entry points:** each script has `--seed` (default from settings) and `--database-url`, matching `scripts/load_hi_small.py`. Each one truncates and reloads only its own tables.

## Tests
- Unit (no DB, no network): same seed gives identical profiles across two runs, different seeds differ, variant generators (reordered, typo) behave as described, selection ratio and the decoy/launderer mix, embedding cache hit and miss logic, and template checks: valid JSON, every `{placeholder}` used is a supported one, every theme has all four parts, and a rendered article contains no unfilled `{...}`.
- Integration (docker Postgres, `aml_test`, same fixture pattern as `tests/integration/test_ingest_slice.py`, loading the existing tiny fixtures):
  - `kyc_profiles` count equals the number of eligible entities.
  - `sanctions_entities` count equals the rows in a small fixture CSV (`tests/fixtures/`, about 20 rows).
  - Every `planted_matches` row is found by `pg_trgm` similarity against `sanctions_entities`.
  - Running each script twice gives identical table contents.
  - Migration `0002` up, down and up again.
- OpenAI embedding calls are replaced by a fake client that returns deterministic vectors. One live test marked `@pytest.mark.live` is skipped by default.

## Acceptance criteria
- Two runs with the same seed produce identical `kyc_profiles`, `planted_matches`, and selected media entity IDs (dumped rows `diff` empty).
- A second `generate_media` run with a warm embedding cache makes zero API calls.
- Two runs with the same seed produce identical article text (dumped `headline` and `body` `diff` empty). Different entities get visibly different articles (no more than about 5% of articles share the same template combination at the full-dataset size, measured on the template choices before names are filled in).
- No article text contains an unfilled placeholder.
- Media covers 2% ± 0.5% of KYC entities, and the result contains both decoys and true launderers.
- All planted names are retrievable by trigram search. Exact copies score 1.0.
- A cosine nearest-neighbour query on `adverse_media.embedding` returns the article whose text was used as the query.
- Migration `0002` runs up and down cleanly.
- `ruff check .`, `mypy .` and `pytest` pass.
- No secrets in the repo, and `data/` stays gitignored.

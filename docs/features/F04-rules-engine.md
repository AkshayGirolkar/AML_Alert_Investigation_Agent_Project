# F04 – Rules Engine and Rules-Only Baseline

## Goal
Deterministic SQL rules scan `transactions` and write `alerts`, each linked to ground truth (`is_laundering`, laundering attempts, typology). A baseline report gives precision and recall per rule and per typology. This is the "rules-only" comparison point for the agent evaluation in F15, and it produces the alerts the agent investigates in F06.

## Source data (from F02/F03, no new downloads)
| Table | Use |
|---|---|
| `transactions` (`txn_id`, `ts`, `from_account_id`, `to_account_id`, `amount_paid`, `payment_currency`, `payment_format`, `is_laundering`) | Rule input. Indexes `(from_account_id, ts)` and `(to_account_id, ts)` already exist. Not partitioned (data spans 18 days). |
| `accounts` → `entities` → `kyc_profiles` (`country`, `risk_rating`, `expected_monthly_volume`) | Customer risk for the high-risk-counterparty rule. |
| `sanctions_entities`, `planted_matches` | Sanctions-match signal for the high-risk-counterparty rule. |
| `laundering_attempts` (`typology`), `laundering_attempt_transactions` | Typology label for true positives. In a full load, 1,968 of 5,177 laundering rows belong to no attempt, so they have no typology. |

Measure on the dev slice before fixing thresholds: velocity and degree distributions, currency mix, amount percentiles. Record the numbers in this spec's Decisions section.

## In scope
- Migration `0003`: `alerts` table (tested up/down/up like `0001`/`0002`), plus an "F04" section in `app/db/models.py`:
  - `alert_id` bigserial PK, `rule_id` text, `account_id` int FK, `window_start`/`window_end` timestamp, `trigger_txn_ids` bigint[], `score` numeric, `created_at`, `is_true_positive` bool, `typology` text null.
  - Unique `(rule_id, account_id, window_start)`. Indexes on `rule_id` and `account_id`.
- Five SQL rules: `high_velocity`, `fan_out` and `fan_in` (two rule ids, so the baseline is per direction), `round_trip` (cycles, 2-3 hops), `structuring` (just under the reporting limit), `high_risk_counterparty` (counterparty in a high-risk country/rating or with a sanctions-name match).
- Engine, labeling, and baseline report (see Interfaces).
- Rule thresholds in `Settings` (`app/config.py`) and `.env.example`.
- Scripts `scripts/run_rules.py` and `scripts/rules_baseline.py`; logic in `app/rules/`.

## Out of scope
Agent and tools (F05/F06), SQS/API/UI, partitioning, Redis, and PEP lists. Alert triage, cases, and the `cases`/`evidence`/`decisions`/`audit_log` tables are also out of scope.

## Interfaces
- `app/rules/config.py`: frozen `RuleConfig` dataclass (validated thresholds), `RuleConfig.from_settings(settings)`, and `ALL_RULE_IDS`.
- `app/rules/engine.py`: `run_rules(database_url, config=None, rule_ids=None, reset=False) -> dict[str, int]` (alerts inserted per rule). Idempotent. `reset=True` deletes only the selected rules' alerts first. Unknown rule ids raise `ValueError`.
- Windows are tumbling buckets aligned to the epoch (`window_start` = bucket start); `window_end - window_start` equals the rule's window length.
- Alert on an account: fan-out/velocity/structuring use the sender; fan-in uses the receiver; round-trip flags every account in the cycle; high-risk-counterparty flags the account on the other side of the risky counterparty, in both directions, and only for first-contact pairs in the window.
- `app/rules/labeling.py`: sets `is_true_positive` and `typology` on alerts. True positive when any trigger txn has `is_laundering`. Typology comes from `laundering_attempt_transactions` (null when untyped).
- `app/rules/baseline.py`: `compute_baseline(database_url) -> BaselineReport` with per-rule and per-typology precision, recall, alert count, FP rate.
- CLI: `python -m scripts.run_rules` and `python -m scripts.rules_baseline`, with `--database-url` and the "Target database" print, as in F02/F03.

## Decisions
- **Alert grain:** one alert per account + rule + time window (confirmed with the owner).
- **Metrics:**
  - Precision = true-positive alerts / all alerts.
  - Recall (transaction level) = laundering txns in at least one alert's `trigger_txn_ids` / all laundering txns.
  - Recall (attempt level) = laundering attempts with at least one covered txn / all attempts.
  - Per-typology recall uses the attempt typology. Laundering rows with no attempt are reported in a separate "untyped" bucket and included in the overall numbers.
- **Currency:** the structuring limit applies to USD payments only; other currencies are excluded from that rule, because no FX conversion exists. Document this as a known limitation.
- **Arithmetic:** all computed in SQL/Python, never by an LLM.
- **Idempotency:** `INSERT ... ON CONFLICT DO NOTHING` on the unique key. Re-running does not duplicate alerts. Labeling is a deterministic UPDATE, safe to repeat. Changing thresholds and re-running requires `--reset` (truncate alerts) to avoid stale rows.
- **Noise:** thresholds are set so each rule produces mostly false positives, as in a real bank. Do not tune them against `is_laundering`, to keep the baseline honest. Record chosen thresholds here.
- **Perf:** check the fan and cycle queries with `EXPLAIN ANALYZE` and use the existing indexes. Add an index only if the plan shows a need.
- **Dev data:** develop on the ~100k slice; run the full dataset only for evals.

## Tests
- Unit (no DB): label logic, baseline metric math (including the untyped bucket and zero-division cases), threshold parsing.
- Integration (docker Postgres, `aml_test`, fixture pattern from `tests/integration/test_synth.py`, tiny fixtures in `tests/fixtures/raw`): per rule, one planted positive and one benign look-alike. The rule fires on the positive and not on its twin.
- Idempotency: two runs give identical alert counts and contents.
- Labeling: planted laundering txns give `is_true_positive` and the right typology; an untyped one gives a null typology.
- Migration `0003` up, down, up.

## Acceptance criteria
- Each rule fires on its planted positive and not on its benign twin.
- Two `run_rules` runs on the slice yield identical alert counts.
- `rules_baseline` runs end to end on the slice and prints per-rule and per-typology precision, recall, alert count and FP rate.
- Migration `0003` runs up and down cleanly.
- `EXPLAIN ANALYZE` output for the heaviest rule is noted in the PR.
- `ruff check .`, `mypy .` and `pytest` pass; `.env.example` updated; no secrets in the repo.

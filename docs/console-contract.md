# Console contract (F10.5)

The only document the separate console project (React + Node) needs. The console shares **only
the Postgres database** with the Python agent: it never calls the FastAPI app.

## Connecting
- Role `console_app`; password = `CONSOLE_DB_PASSWORD` from the agent repo's `.env` (set it before
  running `alembic upgrade head`; with no password the role cannot log in).
- Same database as the agent. Use schema `console` only: `SET search_path = console` or qualify names.
- The role can `SELECT` the four views and `INSERT` into `console.requests`. Everything else is denied.

## Reading: views (a breaking change to any column needs a note here)
| View | Rows | Purpose |
|---|---|---|
| `console.v_alert_queue` | one per alert | the queue: rule, score, window, `case_status` (`not_investigated`, `queued`, `running`, `awaiting_review`, `completed`, `failed`), `outcome` (`closed`, `escalated`), agent `disposition`, `typology`, `risk_score` |
| `console.v_case_detail` | one per case | alert + KYC (`full_name`, `country`, `risk_rating`, ...) + `verdict` (jsonb), `evidence` (jsonb list), `sar_draft`, `escalation_reason`, `error`, and the analyst `decision`, `decided_by`, `decision_notes`, `decided_at` |
| `console.v_alert_transactions` | one per (alert, trigger transaction) | the transactions behind an alert: time, from/to account number and bank, amounts, currencies, payment format; filter by `alert_id` |
| `console.v_requests` | one per request | status and `result` of what the console asked for; filter by `requested_by` |

Ground truth (`is_true_positive`, the IBM typology) is deliberately not exposed.
Status changes are not pushed: poll `v_case_detail` / `v_requests`.

## Writing: `console.requests`
Insert one row; the console generates `request_id` (a uuid), so retrying an insert is safe (a duplicate
id fails the primary key). Only these columns can be set: `request_id, kind, alert_id, case_id,
payload, requested_by`. `requested_by` is the logged-in analyst's id; it is recorded in `audit_log`.

```sql
-- start an investigation
INSERT INTO console.requests (request_id, kind, alert_id, requested_by)
VALUES (gen_random_uuid(), 'investigate', 42, 'analyst-7');

-- approve (or reject) an escalated case that is awaiting_review
INSERT INTO console.requests (request_id, kind, case_id, payload, requested_by)
VALUES (gen_random_uuid(), 'decision', 17,
        '{"decision": "approve", "notes": "Matches the typology"}', 'analyst-7');
```

## Request lifecycle
`pending` -> `applied` or `rejected`, usually within a second (the worker is notified on insert and
also polls every few seconds). `result` holds:
- applied: `{"case_id": 17, "case_status": "awaiting_review"}`
- rejected: `{"code": 404 | 409 | 400 | 500, "error": "..."}`: unknown alert/case (404), the case is not
  awaiting review or already has a different decision (409), malformed payload (400), repeated internal
  failure (500).

An applied decision is not the end of the case: the graph then resumes and `case_status` moves to
`completed` (approve -> `escalated`, reject -> `closed`). If the worker is down, requests simply stay `pending`.

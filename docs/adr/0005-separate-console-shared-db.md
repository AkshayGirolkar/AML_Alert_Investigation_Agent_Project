# ADR 0005 – Separate console that shares only the database

## Status
Accepted

## Context
The project should resemble a bank where an analyst console and its middleware already exist
(different technology, built long ago) and the Python agent is added later. The two systems must
share nothing except the Postgres database. This replaces the Streamlit console planned for F14 and
makes the planned JWT/RBAC in FastAPI (F11) optional.

## Decisions
- **The schema is the contract.** The console is a separate project (React + TypeScript front end, Node
  middleware) that never calls FastAPI. It reads `console.v_*` views through a restricted role. Views
  let the Python tables change without breaking the console, and the role cannot touch base tables.
- **One write path: a request table, not direct writes.** The console inserts into `console.requests`;
  it never writes `cases`, `decisions` or `audit_log`. A relay in the worker validates each request and
  calls the same code as the API (`app/services/case_commands.py`), so the F08 rules (decision only on
  `awaiting_review`, one decision per case, audit row) cannot be bypassed by a second system.
- **Outbox with a client-generated id.** The console generates `request_id` (uuid), so retries are
  idempotent. The relay is at-least-once; correctness rests on the idempotency F07/F08 already have.
- **`LISTEN/NOTIFY` for latency, polling for correctness.** A notification wakes the relay; a poll every
  few seconds covers lost notifications and a relay restart. Claiming uses `FOR UPDATE SKIP LOCKED`,
  so several workers can run the relay.
- **Persist evidence on the case.** Analysts must see what a verdict cites, and evidence was only in the
  LangGraph checkpoint. A JSONB column on `cases` is enough; a normalised `evidence` table can come later.
- **The console owns its auth.** Its users, sessions and roles live in its own tables/schema. The Python
  side records the console-supplied `requested_by` as the actor, and does not authenticate it.
- **Status by polling.** The console polls `v_case_detail` / `v_requests`. Redis SSE (F10) stays an
  internal Python feature; Redis is not shared.
- **`POST /cases/{id}/decision` stays** for tests and the CLI, but is no longer the UI path.

## Consequences
- `requested_by` is trusted, not verified. Acceptable for a portfolio simulation; a real deployment would
  put mutual TLS or a signed claim in the request.
- A view is a stable surface: changing a column in `console.v_*` is a breaking change and needs a
  migration note in `docs/console-contract.md`.
- Decision latency is bounded by the relay (notify, or the poll interval at worst) plus the SQS hop.
- The relay adds one more task to the worker process; if it is disabled, the console can still read but
  its requests wait in `pending`.

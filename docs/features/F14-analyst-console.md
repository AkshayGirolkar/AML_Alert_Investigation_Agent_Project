# F14 – Analyst console (separate project, shares only the database)

## Goal
A web console where analysts see the alert queue, open a case with its evidence, start an
investigation, and approve or reject an escalated case. It stands in for the "old system built
long ago in other technology": its own repo, its own stack and its own login. It talks to the agent
**only through the Postgres contract from F10.5** (`docs/console-contract.md`); it never calls FastAPI.

## Where it lives
- A **separate git repo** next to this one, e.g. `~/aml-console` (name: `aml-console`). Nothing in
  it imports from, or is imported by, the agent repo.
- This repo only holds this spec, ADR 0005 and `docs/console-contract.md`. The console repo gets a
  copy of the contract in its README and treats it as the API definition.
- Own `docker-compose.yml` that starts only the console; it connects to the agent repo's Postgres
  (`localhost:5433`) with the `console_app` role.

## Stack
- Front end: React + TypeScript (Vite), TanStack Query for polling, React Router.
- Middleware: Node 22 + TypeScript + Fastify, plain SQL with `pg` (no ORM: the views are the contract).
- Tests: Vitest (unit + API), Playwright (one browser flow). Lint: ESLint + Prettier. CI: GitHub Actions.

## In scope
- **F14a – middleware (`server/`)**
  - Login with username/password, signed session cookie (httpOnly), logout. Users live in the console's own
    schema `console_auth` (`users`: id, username, password_hash (argon2), created_at), created by the
    console's own migration (run once with an admin DB URL; the app runs as `console_app`, which that
    migration grants access to `console_auth` only). A seed script creates demo analysts.
  - JSON API for the UI (all require login): `GET /api/alerts` (filter by `case_status`, rule; paged,
    from `console.v_alert_queue`), `GET /api/cases/:id` (from `console.v_case_detail`),
    `POST /api/alerts/:id/investigate`, `POST /api/cases/:id/decision`, `GET /api/requests/:id`.
  - The two POSTs insert one row into `console.requests` with a server-generated uuid and
    `requested_by` = the logged-in username (never taken from the browser), and return `202 {request_id}`.
    The UI polls `GET /api/requests/:id` (reads `v_requests`) until `applied` or `rejected`.
  - Input validation (zod); `rejected` results are mapped to clear messages (404/409/400/500).
- **F14b – React UI (`web/`)**
  - Login page; alert queue (status badges, risk score, filters, auto-refresh every few seconds);
    case page (alert and KYC details, verdict with rationale, evidence list where cited evidence ids
    link to the evidence, SAR draft, decision history).
  - "Investigate" button on alerts with `not_investigated` / `failed`; Approve / Reject (with notes) on
    `awaiting_review` cases only; pending/applied/rejected feedback while a request is in flight.
  - Loading, empty and error states; no ground-truth data is shown (the contract does not expose it).
- **Agent-repo addition (small, done first):** migration `0007` adds `console.v_alert_transactions`
  (the transactions behind `alerts.trigger_txn_ids`: id, time, sender/receiver account, amount, currency,
  payment format) and grants `SELECT` to `console_app`; documented in `docs/console-contract.md`.
  The case page shows these. No other agent-repo change.

## Out of scope
Supervisor role and approval tiers, SSO/Cognito, push updates (polling only), editing the SAR draft,
"request more info" decisions, charts of eval results (F15), deployment to AWS (F17), a design system,
mobile layout.

## Interfaces
- Database: only `console.v_*` views (read) and `INSERT` into `console.requests` (write), as in
  `docs/console-contract.md`. Request payloads and result codes are defined there.
- Browser <-> middleware: the JSON API above; cookie session; same-origin (Vite dev proxy in development).
- Config (env, `.env.example` committed): `DATABASE_URL` (role `console_app`), `ADMIN_DATABASE_URL`
  (migrations only), `SESSION_SECRET`, `PORT`.
- Failure policy: if the database is down the API returns 503 and the UI shows a banner; if the agent worker is down,
  requests stay `pending` and the UI says "waiting for the agent" instead of failing.

## Acceptance criteria
- The repo builds, lints and runs its tests with no checkout of the agent repo (it needs only a Postgres that has
  the contract applied; the test setup documents how to get one).
- Unauthenticated calls to any `/api` route (except login) return 401; a wrong password never says which part was wrong.
- The middleware connects only as `console_app`; a test shows a direct write to `cases`/`decisions` fails.
- `requested_by` always equals the session user, even if the request body tries to set it.
- Investigate and decision actions insert exactly one request row, and returning to the page shows the same request
  state (polling is idempotent). Double-clicking a button does not create two decisions (same request id or a disabled button).
- `rejected` results (404, 409, 400) show a readable message; a case that is not `awaiting_review` shows no Approve/Reject.
- The queue refreshes without a page reload; the case page shows verdict, evidence, SAR draft, KYC, trigger transactions and the decision.
- No ground-truth field (`is_true_positive`, IBM typology) appears in any API response or screen.
- One Playwright flow passes against a real Postgres with a stub relay: log in -> investigate -> (stub marks the case
  `awaiting_review`) -> approve -> request shows `applied`.
- Manual demo with the real worker: an alert is investigated, paused, approved in the browser, and ends `completed/escalated`.

## Build order
1. Agent repo: migration `0007` (`v_alert_transactions`) + contract doc update, own small PR.
2. Console repo: scaffold, CI, DB layer and migrations for `console_auth`.
3. F14a middleware with tests, then F14b UI, then the Playwright flow and the manual demo.
4. Write `docs/features/F14-explained.txt` here and update CLAUDE.md.

## Open questions
- Repo location and name (`~/aml-console` assumed) and whether the console repo gets its own Claude Code `CLAUDE.md`
  (recommended: yes, so it stays independent of this one).

# F08 – Human-in-the-loop (interrupt, decision endpoint, resume)

## Goal
Escalated cases pause for analyst review. An analyst approves or rejects via the API, and the worker resumes the paused graph with that decision.

## In scope
- `human_review` graph node after `draft_sar` calling `interrupt()`; resumed with `Command(resume={decision, analyst_id, notes})`.
- New case status `awaiting_review`; tables `decisions` (UNIQUE `case_id`) and `audit_log`; Alembic migration `0005`.
- `POST /cases/{id}/decision` (`approve` | `reject`; body carries `analyst_id` and `notes`).
- Resume over SQS: message `{"type":"resume","case_id","alert_id"}` (absent `type` = `investigate`).
- Worker distinguishes a paused thread (interrupt pending) from a crashed one (F07 resume).
- CLI `python -m app.agent.run` can show the pending interrupt and resume with `--decision`.

## Out of scope
Auth/RBAC (F11), long-term memory and `write_memory` (F09), analyst UI (F14), review of high-risk closures, a "request more info" decision.

## Interfaces
- Interrupt payload: `{alert_id, verdict, sar_draft, escalation_reason}`.
- Resume value: `{decision: "approve"|"reject", analyst_id: str, notes: str|None}`.
- Final outcome: approve -> `escalated`; reject -> `closed`.
- Statuses: `queued -> running -> awaiting_review -> running -> completed` (or `failed`).
- Responses: 404 unknown case; 409 case not `awaiting_review` and no matching decision on file; 200 idempotent repeat of the same decision; 202 accepted.

## Acceptance criteria
- An escalated alert pauses with status `awaiting_review`; no `completed` write happens before a decision.
- Auto-closed alerts behave exactly as in F07.
- Approve and reject each resume the graph to the correct final outcome, stored with analyst id and notes.
- Repeated decision POSTs and duplicate resume messages are no-ops (no second decision, no second audit row).
- A worker restart while paused leaves the case paused; a crash mid-run still resumes as in F07.
- `complete` cannot overwrite a case that is not `running`.
- Migration 0005 upgrades and downgrades cleanly.
- ruff, mypy and pytest pass.

"""F10.5: console schema, request table, read-only views, restricted role, cases.evidence

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-26 12:00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.config import get_settings

revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ROLE = "console_app"

REQUESTS = """
CREATE TABLE console.requests (
    request_id   uuid PRIMARY KEY,
    kind         text NOT NULL CHECK (kind IN ('investigate', 'decision')),
    alert_id     bigint,
    case_id      bigint,
    payload      jsonb NOT NULL DEFAULT '{}'::jsonb,
    requested_by text NOT NULL CHECK (length(requested_by) > 0),
    status       text NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending', 'applied', 'rejected')),
    attempts     integer NOT NULL DEFAULT 0,
    result       jsonb,
    created_at   timestamp NOT NULL DEFAULT now(),
    processed_at timestamp,
    CONSTRAINT ck_console_requests_target CHECK (
        (kind = 'investigate' AND alert_id IS NOT NULL)
        OR (kind = 'decision' AND case_id IS NOT NULL)
    )
)
"""

NOTIFY = """
CREATE FUNCTION console.notify_request() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM pg_notify('console_requests', NEW.request_id::text);
    RETURN NEW;
END
$$
"""

# The console never sees ground truth (alerts.is_true_positive / alerts.typology).
V_ALERT_QUEUE = """
CREATE VIEW console.v_alert_queue AS
SELECT a.alert_id,
       a.rule_id,
       a.account_id,
       a.window_start,
       a.window_end,
       a.score,
       a.created_at AS alert_created_at,
       c.case_id,
       COALESCE(c.status, 'not_investigated') AS case_status,
       c.outcome,
       c.verdict ->> 'disposition' AS disposition,
       c.verdict ->> 'typology' AS typology,
       (c.verdict ->> 'risk_score')::numeric AS risk_score,
       c.updated_at AS case_updated_at
FROM alerts a
LEFT JOIN cases c ON c.alert_id = a.alert_id
"""

V_CASE_DETAIL = """
CREATE VIEW console.v_case_detail AS
SELECT c.case_id,
       c.alert_id,
       c.status AS case_status,
       c.outcome,
       c.verdict,
       c.evidence,
       c.sar_draft,
       c.escalation_reason,
       c.error,
       c.created_at AS case_created_at,
       c.completed_at,
       a.rule_id,
       a.account_id,
       a.window_start,
       a.window_end,
       a.score,
       a.trigger_txn_ids,
       k.entity_id,
       k.full_name,
       k.occupation,
       k.country,
       k.expected_monthly_volume,
       k.risk_rating,
       d.decision,
       d.analyst_id AS decided_by,
       d.notes AS decision_notes,
       d.decided_at
FROM cases c
JOIN alerts a ON a.alert_id = c.alert_id
LEFT JOIN accounts ac ON ac.account_id = a.account_id
LEFT JOIN kyc_profiles k ON k.entity_id = ac.entity_id
LEFT JOIN decisions d ON d.case_id = c.case_id
"""

V_REQUESTS = """
CREATE VIEW console.v_requests AS
SELECT request_id, kind, alert_id, case_id, requested_by, status, result,
       created_at, processed_at
FROM console.requests
"""


def upgrade() -> None:
    op.execute("ALTER TABLE cases ADD COLUMN evidence jsonb")
    op.execute("CREATE SCHEMA console")
    op.execute(REQUESTS)
    op.execute(
        "CREATE INDEX ix_console_requests_pending ON console.requests (created_at) "
        "WHERE status = 'pending'"
    )
    op.execute(NOTIFY)
    op.execute(
        "CREATE TRIGGER trg_console_requests_notify AFTER INSERT ON console.requests "
        "FOR EACH ROW EXECUTE FUNCTION console.notify_request()"
    )
    op.execute(V_ALERT_QUEUE)
    op.execute(V_CASE_DETAIL)
    op.execute(V_REQUESTS)

    # Roles are cluster-wide, so create it only if another database has not already.
    bind = op.get_bind()
    exists = bind.execute(
        sa.text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": ROLE}
    ).first()
    if not exists:
        password = get_settings().console_db_password
        if password:
            stmt = bind.execute(
                sa.text(
                    "SELECT format('CREATE ROLE console_app LOGIN PASSWORD %L', :p)"
                ),
                {"p": password},
            ).scalar_one()
        else:
            stmt = "CREATE ROLE console_app NOLOGIN"
        op.execute(stmt)

    op.execute(f"GRANT USAGE ON SCHEMA console TO {ROLE}")
    op.execute(
        f"GRANT SELECT ON console.v_alert_queue, console.v_case_detail, "
        f"console.v_requests TO {ROLE}"
    )
    # Column-level: the console cannot set status/result/attempts itself.
    op.execute(
        f"GRANT INSERT (request_id, kind, alert_id, case_id, payload, requested_by) "
        f"ON console.requests TO {ROLE}"
    )


def downgrade() -> None:
    op.execute(f"REVOKE ALL ON SCHEMA console FROM {ROLE}")
    op.execute(
        "DROP VIEW console.v_requests, console.v_case_detail, console.v_alert_queue"
    )
    op.execute("DROP TABLE console.requests")
    op.execute("DROP FUNCTION console.notify_request()")
    op.execute("DROP SCHEMA console")
    op.execute("ALTER TABLE cases DROP COLUMN evidence")
    # The role is shared by every database on the cluster: drop it only if nothing else uses it.
    op.execute(
        f"""
        DO $$ BEGIN
            DROP ROLE {ROLE};
        EXCEPTION WHEN dependent_objects_still_exist OR undefined_object THEN NULL;
        END $$
        """
    )

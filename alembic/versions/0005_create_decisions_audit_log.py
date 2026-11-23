"""F08: awaiting_review status, decisions and audit_log tables

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-23 12:00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD = "status IN ('queued','running','completed','failed')"
NEW = "status IN ('queued','running','awaiting_review','completed','failed')"


def upgrade() -> None:
    op.drop_constraint("ck_cases_status", "cases", type_="check")
    op.create_check_constraint("ck_cases_status", "cases", NEW)

    op.create_table(
        "decisions",
        sa.Column("decision_id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("case_id", sa.BigInteger(), nullable=False),
        sa.Column("decision", sa.Text(), nullable=False),
        sa.Column("analyst_id", sa.Text(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "decided_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint(
            "decision IN ('approve','reject')", name="ck_decisions_decision"
        ),
        sa.ForeignKeyConstraint(["case_id"], ["cases.case_id"]),
        sa.PrimaryKeyConstraint("decision_id"),
        sa.UniqueConstraint("case_id"),
    )
    op.create_table(
        "audit_log",
        sa.Column("audit_id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("case_id", sa.BigInteger(), nullable=True),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("detail", postgresql.JSONB(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["case_id"], ["cases.case_id"]),
        sa.PrimaryKeyConstraint("audit_id"),
    )
    op.create_index("ix_audit_log_case_id", "audit_log", ["case_id"])


def downgrade() -> None:
    op.drop_index("ix_audit_log_case_id", table_name="audit_log")
    op.drop_table("audit_log")
    op.drop_table("decisions")
    # The old constraint has no 'awaiting_review'; send those cases back to the queue.
    op.execute("UPDATE cases SET status='queued' WHERE status='awaiting_review'")
    op.drop_constraint("ck_cases_status", "cases", type_="check")
    op.create_check_constraint("ck_cases_status", "cases", OLD)

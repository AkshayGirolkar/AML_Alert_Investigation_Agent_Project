"""create cases table for F07 async investigations

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-15 12:00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "cases",
        sa.Column("case_id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("alert_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "status", sa.Text(), server_default=sa.text("'queued'"), nullable=False
        ),
        sa.Column(
            "attempts", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("outcome", sa.Text(), nullable=True),
        sa.Column("verdict", postgresql.JSONB(), nullable=True),
        sa.Column("sar_draft", postgresql.JSONB(), nullable=True),
        sa.Column("escalation_reason", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "status IN ('queued','running','completed','failed')",
            name="ck_cases_status",
        ),
        sa.ForeignKeyConstraint(["alert_id"], ["alerts.alert_id"]),
        sa.PrimaryKeyConstraint("case_id"),
        sa.UniqueConstraint("alert_id"),
    )
    op.create_index("ix_cases_status", "cases", ["status"])


def downgrade() -> None:
    op.drop_index("ix_cases_status", table_name="cases")
    op.drop_table("cases")

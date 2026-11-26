"""F12: llm_calls (per-call tokens, cost, latency) and a per-case cost view

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-26 12:00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0008"
down_revision: str | Sequence[str] | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "llm_calls",
        sa.Column("call_id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("case_id", sa.BigInteger(), nullable=False),
        sa.Column("call_seq", sa.Integer(), nullable=False),
        sa.Column("node", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("cost_usd", sa.Numeric(12, 6), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("fallback", sa.Boolean(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["case_id"], ["cases.case_id"]),
        sa.PrimaryKeyConstraint("call_id"),
        sa.UniqueConstraint("case_id", "call_seq"),
    )
    op.execute(
        """
        CREATE VIEW llm_case_costs AS
        SELECT case_id,
               count(*) AS calls,
               count(*) FILTER (WHERE error IS NOT NULL) AS failed_calls,
               count(*) FILTER (WHERE fallback) AS fallback_calls,
               sum(input_tokens) AS input_tokens,
               sum(output_tokens) AS output_tokens,
               sum(cost_usd) AS cost_usd,
               sum(latency_ms) AS latency_ms
        FROM llm_calls GROUP BY case_id
        """
    )


def downgrade() -> None:
    op.execute("DROP VIEW llm_case_costs")
    op.drop_table("llm_calls")

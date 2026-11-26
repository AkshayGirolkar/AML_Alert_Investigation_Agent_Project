"""F15: eval_runs and eval_results (evaluation harness output)

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-26 15:00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0009"
down_revision: str | Sequence[str] | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "eval_runs",
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("dataset", sa.Text(), nullable=False),
        sa.Column("git_sha", sa.Text(), nullable=True),
        sa.Column("planner_model", sa.Text(), nullable=False),
        sa.Column("assess_model", sa.Text(), nullable=False),
        sa.Column("config", postgresql.JSONB(), nullable=False),
        sa.Column(
            "started_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("run_id"),
    )
    # No FK to alerts/cases: eval rows must never depend on, or write to, case tables.
    op.create_table(
        "eval_results",
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("alert_id", sa.BigInteger(), nullable=False),
        sa.Column("rule_id", sa.Text(), nullable=False),
        sa.Column("truth", sa.Boolean(), nullable=False),
        sa.Column("truth_typology", sa.Text(), nullable=True),
        sa.Column("outcome", sa.Text(), nullable=True),
        sa.Column("typology", sa.Text(), nullable=True),
        sa.Column("risk", sa.Float(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("grounded", sa.Boolean(), nullable=False),
        sa.Column("n_citations", sa.Integer(), nullable=False),
        sa.Column("tool_calls", sa.Integer(), nullable=False),
        sa.Column("cost_usd", sa.Float(), nullable=False),
        sa.Column("seconds", sa.Float(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("budget_exceeded", sa.Boolean(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("node_costs", postgresql.JSONB(), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["eval_runs.run_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("run_id", "alert_id"),
    )


def downgrade() -> None:
    op.drop_table("eval_results")
    op.drop_table("eval_runs")

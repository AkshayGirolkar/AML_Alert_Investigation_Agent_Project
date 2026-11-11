"""create kyc, sanctions, planted match and adverse media tables

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-11 09:00:00

"""

from collections.abc import Sequence

import pgvector.sqlalchemy
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The migration owns the extensions so tests do not rely on docker init scripts.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    op.create_table(
        "kyc_profiles",
        sa.Column("entity_id", sa.String(length=16), nullable=False),
        sa.Column("full_name", sa.Text(), nullable=False),
        sa.Column("occupation", sa.Text(), nullable=False),
        sa.Column("country", sa.String(length=2), nullable=False),
        sa.Column("expected_monthly_volume", sa.Numeric(20, 2), nullable=False),
        sa.Column("risk_rating", sa.String(length=8), nullable=False),
        sa.CheckConstraint(
            "risk_rating IN ('low','medium','high')", name="ck_kyc_risk_rating"
        ),
        sa.ForeignKeyConstraint(["entity_id"], ["entities.entity_id"]),
        sa.PrimaryKeyConstraint("entity_id"),
    )
    op.create_table(
        "sanctions_entities",
        sa.Column("source_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("aliases", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("schema_type", sa.Text(), nullable=False),
        sa.Column("countries", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("dataset", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("first_seen", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("source_id"),
    )
    op.create_index(
        "ix_sanctions_name_trgm",
        "sanctions_entities",
        ["name"],
        postgresql_using="gin",
        postgresql_ops={"name": "gin_trgm_ops"},
    )
    op.create_table(
        "planted_matches",
        sa.Column("entity_id", sa.String(length=16), nullable=False),
        sa.Column("sanctions_source_id", sa.Text(), nullable=False),
        sa.Column("variant_kind", sa.String(length=16), nullable=False),
        sa.CheckConstraint(
            "variant_kind IN ('exact','reordered','typo')", name="ck_planted_variant"
        ),
        sa.ForeignKeyConstraint(["entity_id"], ["entities.entity_id"]),
        sa.ForeignKeyConstraint(
            ["sanctions_source_id"], ["sanctions_entities.source_id"]
        ),
        sa.PrimaryKeyConstraint("entity_id"),
    )
    op.create_table(
        "adverse_media",
        sa.Column("media_id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("entity_id", sa.String(length=16), nullable=False),
        sa.Column("headline", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("published_at", sa.Date(), nullable=False),
        sa.Column("is_decoy", sa.Boolean(), nullable=False),
        sa.Column("theme", sa.Text(), nullable=False),
        sa.Column("embedding", pgvector.sqlalchemy.Vector(dim=1536), nullable=False),
        sa.ForeignKeyConstraint(["entity_id"], ["entities.entity_id"]),
        sa.PrimaryKeyConstraint("media_id"),
    )
    op.create_index("ix_adverse_media_entity_id", "adverse_media", ["entity_id"])
    op.create_index(
        "ix_adverse_media_embedding",
        "adverse_media",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )


def downgrade() -> None:
    # Extensions are left installed: other objects may depend on them.
    op.drop_table("adverse_media")
    op.drop_table("planted_matches")
    op.drop_table("sanctions_entities")
    op.drop_table("kyc_profiles")

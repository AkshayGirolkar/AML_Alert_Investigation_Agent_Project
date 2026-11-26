"""F16: indexed sanctions aliases (the alias scan was ~2.7 s per screening)

The load test found `sanctions_screen` took ~4 s per cold call, almost all of it the
alias half of the search: `similarity(alias, q)` over every unnested alias (230k rows on
66k entities), unindexed, and it saturated Postgres CPU. `sanctions_aliases` holds one
row per alias with a trigram GIN index, so the tool can use `alias % q` (the same
predicate: similarity >= the session limit) at ~9 ms. Triggers on `sanctions_entities`
keep it in sync for inserts, updates, deletes and TRUNCATE, so loaders need no change.

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-26 16:00:00

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0010"
down_revision: str | Sequence[str] | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "CREATE TABLE sanctions_aliases (source_id text NOT NULL, alias text NOT NULL)"
    )
    op.execute(
        "INSERT INTO sanctions_aliases (source_id, alias) "
        "SELECT s.source_id, al.alias FROM sanctions_entities s "
        "CROSS JOIN LATERAL unnest(s.aliases) AS al(alias)"
    )
    op.execute(
        "CREATE INDEX ix_sanctions_alias_trgm ON sanctions_aliases "
        "USING gin (alias gin_trgm_ops)"
    )
    op.execute(
        "CREATE INDEX ix_sanctions_aliases_source ON sanctions_aliases (source_id)"
    )
    op.execute(
        """
        CREATE FUNCTION sanctions_aliases_sync() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') THEN
                DELETE FROM sanctions_aliases WHERE source_id = OLD.source_id;
            END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') THEN
                INSERT INTO sanctions_aliases (source_id, alias)
                SELECT NEW.source_id, a FROM unnest(NEW.aliases) AS a;
            END IF;
            RETURN NULL;
        END $$
        """
    )
    op.execute(
        "CREATE TRIGGER trg_sanctions_aliases_sync "
        "AFTER INSERT OR UPDATE OR DELETE ON sanctions_entities "
        "FOR EACH ROW EXECUTE FUNCTION sanctions_aliases_sync()"
    )
    op.execute(
        """
        CREATE FUNCTION sanctions_aliases_truncate() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            TRUNCATE sanctions_aliases;
            RETURN NULL;
        END $$
        """
    )
    op.execute(
        "CREATE TRIGGER trg_sanctions_aliases_truncate "
        "AFTER TRUNCATE ON sanctions_entities "
        "FOR EACH STATEMENT EXECUTE FUNCTION sanctions_aliases_truncate()"
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_sanctions_aliases_truncate ON sanctions_entities"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_sanctions_aliases_sync ON sanctions_entities"
    )
    op.execute("DROP FUNCTION IF EXISTS sanctions_aliases_truncate()")
    op.execute("DROP FUNCTION IF EXISTS sanctions_aliases_sync()")
    op.execute("DROP TABLE IF EXISTS sanctions_aliases")

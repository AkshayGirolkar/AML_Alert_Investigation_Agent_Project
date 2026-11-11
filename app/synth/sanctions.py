"""Load the OpenSanctions flat CSV into sanctions_entities (F03).

The file is untrusted data: it is only ever COPYed into an all-TEXT staging
table and never executed or interpolated into SQL.
"""

from pathlib import Path

import psycopg

from app.db.copy import conninfo, copy_file, log

LOADABLE_SCHEMAS = ("Person", "Organization", "LegalEntity", "Company")
DEFAULT_CSV = Path("data/opensanctions/targets.simple.csv")


class SanctionsLoadError(RuntimeError):
    pass


def load_sanctions(database_url: str, csv_path: Path) -> int:
    """Truncate and reload sanctions_entities; returns rows loaded."""
    if not csv_path.is_file():
        raise SanctionsLoadError(f"missing sanctions file: {csv_path}")
    with psycopg.connect(conninfo(database_url)) as conn, conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS stg_sanctions")
        cur.execute(
            """CREATE UNLOGGED TABLE stg_sanctions (
                id text, schema text, name text, aliases text, birth_date text,
                countries text, addresses text, identifiers text, sanctions text,
                phones text, emails text, program_ids text, dataset text,
                first_seen text, last_seen text, last_change text)"""
        )
        copy_file(
            cur, "COPY stg_sanctions FROM STDIN (FORMAT csv, HEADER true)", csv_path
        )
        # planted_matches references sanctions_entities, so it is cleared too.
        cur.execute("TRUNCATE planted_matches, sanctions_entities")
        cur.execute(
            """INSERT INTO sanctions_entities
               (source_id, name, aliases, schema_type, countries, dataset, first_seen)
               SELECT id, name,
                      coalesce(string_to_array(nullif(aliases, ''), ';'), '{}'),
                      schema,
                      coalesce(string_to_array(nullif(countries, ''), ';'), '{}'),
                      coalesce(string_to_array(nullif(dataset, ''), ';'), '{}'),
                      nullif(first_seen, '')::timestamp
               FROM stg_sanctions
               WHERE schema = ANY(%s) AND nullif(trim(name), '') IS NOT NULL
               ORDER BY id""",
            (list(LOADABLE_SCHEMAS),),
        )
        n = cur.rowcount
        cur.execute("DROP TABLE stg_sanctions")
        conn.commit()
    log(f"sanctions_entities: {n:,} rows")
    return n

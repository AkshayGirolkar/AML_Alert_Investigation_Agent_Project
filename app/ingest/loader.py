"""Bulk loader for the IBM HI-Small dataset (F02).

Flow: COPY raw files into UNLOGGED all-TEXT staging tables, normalize with
INSERT ... SELECT, then drop staging. The whole run is one DB transaction, and
every run truncates the six target tables first, so it is repeatable.
"""

import csv
import time
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import psycopg
from sqlalchemy import Table
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import make_url
from sqlalchemy.schema import CreateIndex

from app.db.models import Transaction
from app.ingest.parsing import PATTERN_LINE_FIELDS, parse_patterns_file

TRANS_FILE = "HI-Small_Trans.csv"
ACCOUNTS_FILE = "HI-Small_accounts.csv"
PATTERNS_FILE = "HI-Small_Patterns.txt"

TARGET_TABLES = (
    "laundering_attempt_transactions",
    "laundering_attempts",
    "transactions",
    "accounts",
    "entities",
    "banks",
)

# The CSV header repeats "Account", so staging always uses an explicit column list.
_TXN_COLS = (
    "ts, from_bank, from_acct, to_bank, to_acct, amt_recv, recv_cur, "
    "amt_paid, pay_cur, fmt, is_laundering"
)


class LoadError(RuntimeError):
    pass


@dataclass(frozen=True)
class LoadOptions:
    data_dir: Path
    database_url: str
    slice_mode: bool
    seed: int = 42
    slice_size: int = 100_000


def _conninfo(sqlalchemy_url: str) -> str:
    url = make_url(sqlalchemy_url).set(drivername="postgresql")
    return url.render_as_string(hide_password=False)


def _log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _copy_file(cur: psycopg.Cursor, sql: str, path: Path) -> None:
    with path.open("rb") as fh, cur.copy(sql) as copy:
        while chunk := fh.read(1 << 20):
            copy.write(chunk)


def _scalar(cur: psycopg.Cursor, sql: str, params: tuple = ()) -> int:
    row = cur.execute(sql, params).fetchone()
    assert row is not None
    return int(row[0])


def _create_staging(cur: psycopg.Cursor) -> None:
    cur.execute("DROP TABLE IF EXISTS stg_trans, stg_accounts, stg_pattern_lines")
    cur.execute(
        """CREATE UNLOGGED TABLE stg_accounts (
            bank_name text, bank_id text, account_number text,
            entity_id text, entity_name text)"""
    )
    cur.execute(
        """CREATE UNLOGGED TABLE stg_trans (
            row_no bigint GENERATED ALWAYS AS IDENTITY,
            ts text, from_bank text, from_acct text, to_bank text, to_acct text,
            amt_recv text, recv_cur text, amt_paid text, pay_cur text,
            fmt text, is_laundering text)"""
    )
    cur.execute(
        """CREATE UNLOGGED TABLE stg_pattern_lines (
            line_no serial, attempt_id int,
            ts text, from_bank text, from_acct text, to_bank text, to_acct text,
            amt_recv text, recv_cur text, amt_paid text, pay_cur text,
            fmt text, is_laundering text)"""
    )


def _check_first_row(cur: psycopg.Cursor, trans_path: Path) -> None:
    """Sanity-check that identity row_no follows file order."""
    with trans_path.open(newline="", encoding="utf-8") as fh:
        reader = csv.reader(fh)
        next(reader)
        first = next(reader)
        n_rows = 1 + sum(1 for _ in reader)
    got = cur.execute(
        "SELECT ts, from_acct, to_acct FROM stg_trans WHERE row_no = 1"
    ).fetchone()
    if got != (first[0], first[2], first[4]):
        raise LoadError(f"staging row_no 1 does not match first CSV row: {got}")
    staged = _scalar(cur, "SELECT count(*) FROM stg_trans")
    max_no = _scalar(cur, "SELECT max(row_no) FROM stg_trans")
    if not staged == max_no == n_rows:
        raise LoadError(
            f"row count mismatch: csv={n_rows} staged={staged} max={max_no}"
        )


def _load_attempts_and_match(cur: psycopg.Cursor, patterns_path: Path) -> int:
    """Insert laundering_attempts, stage pattern lines, match them to txns."""
    n_lines = 0
    with cur.copy(
        "COPY stg_pattern_lines (attempt_id, ts, from_bank, from_acct, to_bank, "
        "to_acct, amt_recv, recv_cur, amt_paid, pay_cur, fmt, is_laundering) "
        "FROM STDIN"
    ) as copy:
        attempts = []
        for attempt in parse_patterns_file(str(patterns_path)):
            attempts.append(attempt)
            for fields in attempt.lines:
                assert len(fields) == PATTERN_LINE_FIELDS
                copy.write_row([attempt.attempt_id, *fields])
                n_lines += 1
    cur.executemany(
        "INSERT INTO laundering_attempts "
        "(attempt_id, typology, detail_raw, max_degree_or_hops) "
        "VALUES (%s, %s, %s, %s)",
        [
            (
                a.attempt_id,
                a.header.typology,
                a.header.detail_raw,
                a.header.max_degree_or_hops,
            )
            for a in attempts
        ],
    )
    # Match on all 11 fields; banks compared as ints, amounts as numerics.
    cur.execute(
        """CREATE TEMP TABLE pattern_match AS
        SELECT p.line_no, p.attempt_id, t.row_no AS txn_id
        FROM stg_pattern_lines p
        JOIN stg_trans t ON t.ts = p.ts
         AND t.from_bank::int = p.from_bank::int AND t.from_acct = p.from_acct
         AND t.to_bank::int = p.to_bank::int AND t.to_acct = p.to_acct
         AND t.amt_recv::numeric = p.amt_recv::numeric AND t.recv_cur = p.recv_cur
         AND t.amt_paid::numeric = p.amt_paid::numeric AND t.pay_cur = p.pay_cur
         AND t.fmt = p.fmt AND t.is_laundering = '1'"""
    )
    bad = _scalar(
        cur,
        """SELECT count(*) FROM (
            SELECT p.line_no FROM stg_pattern_lines p
            LEFT JOIN pattern_match m USING (line_no)
            GROUP BY p.line_no HAVING count(m.txn_id) <> 1) x""",
    )
    if bad:
        raise LoadError(
            f"{bad} pattern lines matched zero or multiple laundering transactions"
        )
    distinct_links = _scalar(
        cur,
        "SELECT count(*) FROM (SELECT DISTINCT attempt_id, txn_id "
        "FROM pattern_match) x",
    )
    if distinct_links != n_lines:
        raise LoadError(
            f"pattern lines collapse to {distinct_links} unique links, "
            f"expected {n_lines}"
        )
    return n_lines


def _choose_kept(cur: psycopg.Cursor, opts: LoadOptions) -> None:
    cur.execute("CREATE TEMP TABLE kept (txn_id bigint PRIMARY KEY)")
    if not opts.slice_mode:
        cur.execute("INSERT INTO kept SELECT row_no FROM stg_trans")
        return
    cur.execute(
        "INSERT INTO kept SELECT row_no FROM stg_trans WHERE is_laundering = '1' "
        "UNION SELECT txn_id FROM pattern_match"
    )
    have = _scalar(cur, "SELECT count(*) FROM kept")
    need = max(opts.slice_size - have, 0)
    cur.execute(
        """INSERT INTO kept
        SELECT row_no FROM stg_trans
        WHERE is_laundering <> '1'
        ORDER BY md5(%s::text || row_no::text)
        LIMIT %s""",
        (opts.seed, need),
    )


def _load_core(cur: psycopg.Cursor, opts: LoadOptions) -> None:
    cur.execute(
        """CREATE TEMP TABLE acct_src AS
        SELECT bank_name, bank_id::int AS bank_id, account_number,
               entity_id, entity_name FROM stg_accounts"""
    )
    # Source assumptions the schema depends on: fail loudly if they break.
    checks = {
        "duplicate (bank_id, account_number) in accounts file": """
            SELECT count(*) - count(DISTINCT (bank_id, account_number))
            FROM acct_src""",
        "bank_id with more than one bank name": """
            SELECT count(*) FROM (SELECT bank_id FROM acct_src
            GROUP BY bank_id HAVING count(DISTINCT bank_name) > 1) x""",
        "entity_id with more than one name": """
            SELECT count(*) FROM (SELECT entity_id FROM acct_src
            GROUP BY entity_id HAVING count(DISTINCT entity_name) > 1) x""",
        "entity name without ' #'": """
            SELECT count(*) FROM acct_src WHERE position(' #' in entity_name) = 0""",
    }
    for label, sql in checks.items():
        n = _scalar(cur, sql)
        if n:
            raise LoadError(f"{n}: {label}")

    cur.execute("CREATE TEMP TABLE kept_accts (bank_id int, account_number text)")
    if opts.slice_mode:
        cur.execute(
            """INSERT INTO kept_accts
            SELECT t.from_bank::int, t.from_acct FROM stg_trans t
              JOIN kept k ON k.txn_id = t.row_no
            UNION
            SELECT t.to_bank::int, t.to_acct FROM stg_trans t
              JOIN kept k ON k.txn_id = t.row_no"""
        )
    else:
        cur.execute(
            "INSERT INTO kept_accts SELECT bank_id, account_number FROM acct_src"
        )
    cur.execute("CREATE INDEX ON kept_accts (bank_id, account_number)")
    cur.execute(
        """CREATE TEMP TABLE acct_keep AS
        SELECT a.* FROM acct_src a
        JOIN kept_accts k USING (bank_id, account_number)"""
    )

    cur.execute(
        """INSERT INTO banks (bank_id, bank_name)
        SELECT bank_id, min(bank_name) FROM acct_keep GROUP BY bank_id"""
    )
    cur.execute(
        """INSERT INTO entities (entity_id, entity_name, entity_type)
        SELECT entity_id, min(entity_name),
               split_part(min(entity_name), ' #', 1)
        FROM acct_keep GROUP BY entity_id"""
    )
    cur.execute(
        """INSERT INTO accounts (bank_id, account_number, entity_id)
        SELECT bank_id, account_number, entity_id FROM acct_keep
        ORDER BY bank_id, account_number"""
    )
    _log("banks/entities/accounts loaded; loading transactions")
    cur.execute("SET LOCAL timezone = 'UTC'")  # to_timestamp -> timestamp is exact
    cur.execute(
        """INSERT INTO transactions
        (txn_id, ts, from_account_id, to_account_id, amount_received,
         receiving_currency, amount_paid, payment_currency, payment_format,
         is_laundering)
        SELECT t.row_no,
               to_timestamp(t.ts, 'YYYY/MM/DD HH24:MI')::timestamp,
               fa.account_id, ta.account_id,
               t.amt_recv::numeric, t.recv_cur, t.amt_paid::numeric, t.pay_cur,
               t.fmt, t.is_laundering = '1'
        FROM stg_trans t
        JOIN kept k ON k.txn_id = t.row_no
        JOIN accounts fa ON fa.bank_id = t.from_bank::int
                        AND fa.account_number = t.from_acct
        JOIN accounts ta ON ta.bank_id = t.to_bank::int
                        AND ta.account_number = t.to_acct"""
    )
    kept = _scalar(cur, "SELECT count(*) FROM kept")
    loaded = _scalar(cur, "SELECT count(*) FROM transactions")
    if kept != loaded:
        raise LoadError(f"{kept - loaded} kept transactions had unmatched accounts")
    cur.execute(
        """INSERT INTO laundering_attempt_transactions (attempt_id, txn_id)
        SELECT attempt_id, txn_id FROM pattern_match"""
    )


def load(opts: LoadOptions) -> dict[str, int]:
    """Run the load; returns final row counts."""
    trans_path = opts.data_dir / TRANS_FILE
    accounts_path = opts.data_dir / ACCOUNTS_FILE
    patterns_path = opts.data_dir / PATTERNS_FILE
    for p in (trans_path, accounts_path, patterns_path):
        if not p.is_file():
            raise LoadError(f"missing input file: {p}")

    txn_table = cast(Table, Transaction.__table__)
    index_names = [idx.name for idx in txn_table.indexes]
    index_ddl = [
        str(CreateIndex(idx).compile(dialect=postgresql.dialect()))
        for idx in txn_table.indexes
    ]

    with psycopg.connect(_conninfo(opts.database_url)) as conn, conn.cursor() as cur:
        _log("truncating target tables, dropping secondary txn indexes")
        cur.execute(f"TRUNCATE {', '.join(TARGET_TABLES)} RESTART IDENTITY CASCADE")
        for name in index_names:
            cur.execute(f'DROP INDEX IF EXISTS "{name}"')

        _create_staging(cur)
        _log("COPY accounts file")
        _copy_file(
            cur,
            "COPY stg_accounts (bank_name, bank_id, account_number, entity_id, "
            "entity_name) FROM STDIN (FORMAT csv, HEADER true)",
            accounts_path,
        )
        _log("COPY transactions file")
        _copy_file(
            cur,
            f"COPY stg_trans ({_TXN_COLS}) FROM STDIN (FORMAT csv, HEADER true)",
            trans_path,
        )
        _check_first_row(cur, trans_path)
        _log("matching laundering attempts")
        links = _load_attempts_and_match(cur, patterns_path)
        _choose_kept(cur, opts)
        _load_core(cur, opts)

        _log("recreating indexes")
        for ddl in index_ddl:
            cur.execute(ddl)
        cur.execute("DROP TABLE stg_trans, stg_accounts, stg_pattern_lines")
        conn.commit()
        # Statistics are refreshed after the load transaction has committed.
        cur.execute("ANALYZE")
        conn.commit()

        counts = {
            t: _scalar(cur, f"SELECT count(*) FROM {t}")
            for t in reversed(TARGET_TABLES)
        }
        counts["laundering_rows"] = _scalar(
            cur, "SELECT count(*) FROM transactions WHERE is_laundering"
        )
        if counts["laundering_attempt_transactions"] != links:
            raise LoadError("link count changed after load")
        return counts

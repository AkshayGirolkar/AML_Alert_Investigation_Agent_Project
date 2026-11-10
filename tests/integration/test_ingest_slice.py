"""Integration test for the F02 loader against the Docker Postgres.

Uses a separate `aml_test` database because the loader TRUNCATES its targets.
Skipped when Postgres is not reachable.
"""

import os
import shutil
from pathlib import Path

import psycopg
import pytest
from alembic.config import Config
from sqlalchemy.engine import make_url

from alembic import command
from app.config import get_settings
from app.ingest.loader import LoadError, LoadOptions, load

FIXTURES = Path(__file__).parent.parent / "fixtures" / "raw"
ROOT = Path(__file__).parent.parent.parent
TEST_DB = "aml_test"


@pytest.fixture(scope="module")
def test_url() -> str:
    base = make_url(get_settings().sync_database_url)
    admin = base.set(drivername="postgresql").render_as_string(hide_password=False)
    try:
        with psycopg.connect(admin, autocommit=True, connect_timeout=3) as conn:
            exists = conn.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s", (TEST_DB,)
            ).fetchone()
            if not exists:
                conn.execute(f"CREATE DATABASE {TEST_DB}")
    except psycopg.OperationalError:
        pytest.skip("Postgres is not reachable")
    url = base.set(database=TEST_DB).render_as_string(hide_password=False)
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    # alembic/env.py takes its URL from app settings, so point settings at the
    # test database via the environment (env vars beat .env) for the upgrade.
    prev = os.environ.get("POSTGRES_DB")
    os.environ["POSTGRES_DB"] = TEST_DB
    try:
        command.upgrade(cfg, "head")
    finally:
        if prev is None:
            del os.environ["POSTGRES_DB"]
        else:
            os.environ["POSTGRES_DB"] = prev
    return url


def _opts(url: str, data_dir: Path = FIXTURES, **kw: object) -> LoadOptions:
    return LoadOptions(data_dir=data_dir, database_url=url, slice_mode=True, **kw)  # type: ignore[arg-type]


def _q(url: str, sql: str) -> list[tuple]:
    conninfo = make_url(url).set(drivername="postgresql")
    with psycopg.connect(conninfo.render_as_string(hide_password=False)) as conn:
        return conn.execute(sql).fetchall()


def test_slice_load(test_url: str) -> None:
    counts = load(_opts(test_url, slice_size=100))
    assert counts["transactions"] == 8
    assert counts["laundering_rows"] == 3
    assert counts["laundering_attempts"] == 2
    assert counts["laundering_attempt_transactions"] == 3
    assert counts["accounts"] == 5
    assert counts["banks"] == 3
    assert counts["entities"] == 4

    # txn_id == data row number in the CSV
    assert _q(test_url, "SELECT ts::text FROM transactions WHERE txn_id = 2") == [
        ("2022-09-01 00:25:00",)
    ]
    # no NULL account ids; same account number at two banks stays distinct
    assert _q(
        test_url,
        "SELECT count(*) FROM transactions "
        "WHERE from_account_id IS NULL OR to_account_id IS NULL",
    ) == [(0,)]
    # decoy row 6 has the same 11 fields as pattern line 1 except is_laundering
    assert _q(
        test_url,
        "SELECT txn_id FROM laundering_attempt_transactions ORDER BY 1",
    ) == [(2,), (3,), (8,)]
    # tiny amount survives as NUMERIC
    assert _q(
        test_url, "SELECT amount_paid::text FROM transactions WHERE txn_id=4"
    ) == [("0.00000100",)]
    assert _q(
        test_url,
        "SELECT entity_type FROM entities WHERE entity_id = 'ENT3'",
    ) == [("Sole Proprietorship",)]
    # secondary indexes were recreated
    assert _q(
        test_url,
        "SELECT count(*) FROM pg_indexes WHERE tablename = 'transactions' "
        "AND indexname IN ('ix_transactions_from_ts','ix_transactions_to_ts',"
        "'ix_transactions_laundering')",
    ) == [(3,)]


def test_loader_is_repeatable(test_url: str) -> None:
    first = load(_opts(test_url))
    second = load(_opts(test_url))
    assert first == second


def test_slice_samples_deterministically(test_url: str) -> None:
    # slice smaller than the laundering set + 1 normal row
    c1 = load(_opts(test_url, slice_size=4, seed=7))
    ids1 = _q(test_url, "SELECT txn_id FROM transactions ORDER BY 1")
    c2 = load(_opts(test_url, slice_size=4, seed=7))
    ids2 = _q(test_url, "SELECT txn_id FROM transactions ORDER BY 1")
    assert c1["transactions"] == 4 == c2["transactions"]
    assert ids1 == ids2


def test_unmatched_pattern_line_fails_loudly(test_url: str, tmp_path: Path) -> None:
    for f in FIXTURES.iterdir():
        shutil.copy(f, tmp_path / f.name)
    with (tmp_path / "HI-Small_Patterns.txt").open("a") as fh:
        fh.write(
            "BEGIN LAUNDERING ATTEMPT - STACK\n"
            "2022/09/09 09:09,010,ACC000001,010,ACC000002,1.00,Euro,1.00,Euro,ACH,1\n"
            "END LAUNDERING ATTEMPT - STACK\n"
        )
    before = _q(test_url, "SELECT count(*) FROM transactions")
    with pytest.raises(LoadError):
        load(_opts(test_url, data_dir=tmp_path))
    # single transaction: the previous load is untouched
    assert _q(test_url, "SELECT count(*) FROM transactions") == before

"""Shared bulk-load helpers (promoted from the F02 loader in F03)."""

import time
from pathlib import Path

import psycopg
from sqlalchemy.engine import make_url


def conninfo(sqlalchemy_url: str) -> str:
    url = make_url(sqlalchemy_url).set(drivername="postgresql")
    return url.render_as_string(hide_password=False)


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def copy_file(cur: psycopg.Cursor, sql: str, path: Path) -> None:
    with path.open("rb") as fh, cur.copy(sql) as copy:
        while chunk := fh.read(1 << 20):
            copy.write(chunk)

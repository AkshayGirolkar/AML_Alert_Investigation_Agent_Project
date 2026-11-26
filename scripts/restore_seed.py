"""Restore the data tables from an S3 pg_dump into the (migrated) database (F17).

Runs inside an ECS task (see scripts/aws_seed.sh restore); it needs the image's
postgresql-client-17, the task role's S3 read access and the usual POSTGRES_* env.

  python -m scripts.restore_seed --bucket B [--key seed/aws_seed.dump]

Refuses to run when `accounts` already has rows, because a data-only restore into
populated tables would collide on primary keys. Triggers stay enabled, so
`sanctions_aliases` is rebuilt from `sanctions_entities` (migration 0010).
"""

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import boto3  # type: ignore[import-untyped]
import psycopg

from app.config import get_settings
from app.db.copy import conninfo


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--bucket", default=os.environ.get("S3_DATA_BUCKET"))
    p.add_argument("--key", default="seed/aws_seed.dump")
    p.add_argument("--jobs", type=int, default=2)
    a = p.parse_args()
    if not a.bucket:
        print("--bucket (or S3_DATA_BUCKET) is required")
        return 2

    s = get_settings()
    with psycopg.connect(conninfo(s.sync_database_url)) as conn:
        n = conn.execute("select count(*) from accounts").fetchone()
        if n and n[0] > 0:
            print(f"accounts already has {n[0]} rows; refusing to restore twice")
            return 3

    with tempfile.TemporaryDirectory() as tmp:
        dump = Path(tmp) / "seed.dump"
        print(f"Downloading s3://{a.bucket}/{a.key}", flush=True)
        boto3.client("s3", region_name=s.aws_region).download_file(
            a.bucket, a.key, str(dump)
        )
        print(f"Restoring {dump.stat().st_size / 1e6:.0f} MB", flush=True)
        env = {**os.environ, "PGPASSWORD": s.postgres_password, "PGSSLMODE": "require"}
        cmd = [
            "pg_restore",
            "--data-only",
            "--no-owner",
            "--exit-on-error",
            f"--jobs={a.jobs}",
            f"--host={s.postgres_host}",
            f"--port={s.postgres_port}",
            f"--username={s.postgres_user}",
            f"--dbname={s.postgres_db}",
            str(dump),
        ]
        rc = subprocess.run(cmd, env=env, check=False).returncode
    print("restore exit code", rc, flush=True)
    return rc


if __name__ == "__main__":
    sys.exit(main())

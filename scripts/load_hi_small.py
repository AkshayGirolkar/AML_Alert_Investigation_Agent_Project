"""Load the IBM HI-Small dataset into Postgres.

.venv/bin/python -m scripts.load_hi_small --slice
.venv/bin/python -m scripts.load_hi_small --full
"""

import argparse
from pathlib import Path

from sqlalchemy.engine import make_url

from app.config import get_settings
from app.ingest.loader import LoadError, LoadOptions, load


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--slice", action="store_true", help="dev slice (~100k txns)")
    mode.add_argument("--full", action="store_true", help="all 5M transactions")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--slice-size", type=int, default=100_000)
    parser.add_argument("--data-dir", type=Path, default=Path("data/raw"))
    parser.add_argument("--database-url", default=None)
    args = parser.parse_args()

    url = args.database_url or get_settings().sync_database_url
    print(f"Target database: {make_url(url).database} (tables will be TRUNCATED)")
    try:
        counts = load(
            LoadOptions(
                data_dir=args.data_dir,
                database_url=url,
                slice_mode=args.slice,
                seed=args.seed,
                slice_size=args.slice_size,
            )
        )
    except LoadError as exc:
        print(f"LOAD FAILED (nothing was committed): {exc}")
        return 1
    for name, n in counts.items():
        print(f"{name:35s} {n:>12,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

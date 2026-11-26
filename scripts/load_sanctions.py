"""Load OpenSanctions targets.simple.csv (truncates sanctions_entities).

.venv/bin/python -m scripts.load_sanctions [--csv data/opensanctions/targets.simple.csv]
"""

import argparse
from pathlib import Path

from sqlalchemy.engine import make_url

from app.config import Settings, get_settings
from app.synth.sanctions import SanctionsLoadError, load_sanctions


def _invalidate_cache(s: Settings) -> None:
    """Cached screening results are stale after a reload (F10); skip quietly if no Redis."""
    import asyncio

    from redis.exceptions import RedisError

    from app.redis_client import make_redis
    from app.services.sanctions_cache import bump_version

    async def go() -> None:
        r = make_redis(s)
        try:
            print(f"sanctions cache version -> {await bump_version(r)}")
        finally:
            await r.aclose()

    try:
        asyncio.run(go())
    except (RedisError, OSError):
        print("Redis not reachable; cache not invalidated (entries expire by TTL)")


def main() -> int:
    s = get_settings()
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--csv", type=Path, default=s.data_dir / "opensanctions/targets.simple.csv"
    )
    p.add_argument("--seed", type=int, default=s.seed, help="unused; for CLI symmetry")
    p.add_argument("--database-url", default=None)
    a = p.parse_args()
    url = a.database_url or s.sync_database_url
    print(
        f"Target database: {make_url(url).database} (sanctions tables will be TRUNCATED)"
    )
    try:
        print(f"{load_sanctions(url, a.csv):,} rows loaded")
        _invalidate_cache(s)
    except SanctionsLoadError as exc:
        print(f"LOAD FAILED: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

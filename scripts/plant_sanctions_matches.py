"""Plant sanctioned names into KYC profiles (regenerates KYC first).

.venv/bin/python -m scripts.plant_sanctions_matches [--count 15]
"""

import argparse

from sqlalchemy.engine import make_url

from app.config import get_settings
from app.synth.planter import plant_matches


def main() -> int:
    s = get_settings()
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--seed", type=int, default=s.seed)
    p.add_argument("--count", type=int, default=15)
    p.add_argument("--database-url", default=None)
    a = p.parse_args()
    url = a.database_url or s.sync_database_url
    print(
        f"Target database: {make_url(url).database} (kyc_profiles and planted_matches rewritten)"
    )
    print(f"{plant_matches(url, a.seed, a.count)} matches planted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

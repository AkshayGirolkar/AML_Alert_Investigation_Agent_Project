"""Generate seeded KYC profiles (truncates kyc_profiles and planted_matches).

.venv/bin/python -m scripts.generate_kyc
"""

import argparse

from sqlalchemy.engine import make_url

from app.config import get_settings
from app.synth.kyc import generate_kyc


def main() -> int:
    s = get_settings()
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--seed", type=int, default=s.seed)
    p.add_argument("--database-url", default=None)
    a = p.parse_args()
    url = a.database_url or s.sync_database_url
    print(f"Target database: {make_url(url).database} (kyc_profiles will be TRUNCATED)")
    print(f"{generate_kyc(url, a.seed):,} profiles")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

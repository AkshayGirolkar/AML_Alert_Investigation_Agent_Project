"""Run the F04 rules engine and write alerts.

.venv/bin/python -m scripts.run_rules [--rules high_velocity fan_out] [--reset]
"""

import argparse

from sqlalchemy.engine import make_url

from app.config import get_settings
from app.rules.config import ALL_RULE_IDS
from app.rules.engine import run_rules


def main() -> int:
    s = get_settings()
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--rules", nargs="+", choices=ALL_RULE_IDS, default=None)
    p.add_argument(
        "--reset",
        action="store_true",
        help="delete the selected rules' alerts first (use after changing thresholds)",
    )
    p.add_argument("--database-url", default=None)
    a = p.parse_args()
    url = a.database_url or s.sync_database_url
    print(
        f"Target database: {make_url(url).database} (alerts inserted; --reset deletes)"
    )
    counts = run_rules(url, rule_ids=a.rules, reset=a.reset)
    print(f"{sum(counts.values())} new alerts: {counts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

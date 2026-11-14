"""Print the rules-only baseline (precision/recall per rule and per typology).

.venv/bin/python -m scripts.rules_baseline
"""

import argparse

from sqlalchemy.engine import make_url

from app.config import get_settings
from app.rules.baseline import compute_baseline, format_report


def main() -> int:
    s = get_settings()
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--database-url", default=None)
    a = p.parse_args()
    url = a.database_url or s.sync_database_url
    print(f"Target database: {make_url(url).database} (read-only)")
    print(format_report(compute_baseline(url)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

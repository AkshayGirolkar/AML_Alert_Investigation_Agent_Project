"""Build the frozen eval set from the labeled alerts (F15) and write it to eval/datasets/.

.venv/bin/python -m scripts.eval_build_dataset --size 100 --seed 42
The file is committed; rebuilding with the same seed on the same alerts gives the same file.
"""

import argparse
from collections import Counter

from sqlalchemy.engine import make_url

from app.config import get_settings
from app.eval.dataset import build_dataset


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--size", type=int, default=100)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--name", default=None, help="default: eval-<size>")
    p.add_argument("--out", default=None, help="default: eval/datasets/<name>.json")
    p.add_argument("--database-url", default=None)
    a = p.parse_args()
    url = a.database_url or get_settings().sync_database_url
    name = a.name or f"eval-{a.size}"
    out = a.out or f"eval/datasets/{name}.json"
    print(f"Target database: {make_url(url).database} (read-only)")
    ds = build_dataset(url, a.size, a.seed, name)
    ds.save(out)
    print(f"Wrote {len(ds.items)} alerts to {out}")
    print("Strata:", dict(sorted(ds.strata.items())))
    print("True alerts:", sum(i.truth for i in ds.items))
    print("Rules:", dict(Counter(i.rule_id for i in ds.items)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

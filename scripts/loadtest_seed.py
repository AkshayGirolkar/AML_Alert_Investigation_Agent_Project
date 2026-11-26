"""Build or reset the F16 load set.

.venv/bin/python -m scripts.loadtest_seed --count 1000          # build (or show) the set
.venv/bin/python -m scripts.loadtest_seed --reset               # delete its cases/usage/checkpoints
The manifest (loadtest/datasets/<name>.json) is reused if it exists, so reruns send the
same alerts; --rebuild picks a fresh set. Eval-set alerts and alerts that already have a
case are never selected. Refuses a non-local database unless --force.
"""

import argparse
from pathlib import Path

from sqlalchemy.engine import make_url

from app.config import get_settings
from app.loadtest.seed import (
    LoadSet,
    build_load_set,
    eval_alert_ids,
    is_local,
    reset,
)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--count", type=int, default=1000)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--name", default=None, help="default: load-<count>")
    p.add_argument("--eval-dataset", default="eval/datasets/eval-100.json")
    p.add_argument("--rebuild", action="store_true", help="ignore an existing manifest")
    p.add_argument("--reset", action="store_true", help="delete the set's rows")
    p.add_argument("--force", action="store_true", help="allow a non-local database")
    p.add_argument("--database-url", default=None)
    a = p.parse_args()

    url = a.database_url or get_settings().sync_database_url
    name = a.name or f"load-{a.count}"
    path = Path(f"loadtest/datasets/{name}.json")
    target = make_url(url)
    print(f"Target database: {target.database} on {target.host}")
    if not is_local(url) and not a.force:
        print("Refusing: the database is not local (use --force to override).")
        return 2

    eval_ids = eval_alert_ids(a.eval_dataset)
    if a.reset:
        if not path.exists():
            print(f"No manifest at {path}; nothing to reset.")
            return 1
        load_set = LoadSet.load(path)
        counts = reset(url, load_set.alert_ids, eval_ids)
        print(f"Reset {len(load_set.alert_ids)} alerts:", counts)
        return 0

    if path.exists() and not a.rebuild:
        load_set = LoadSet.load(path)
        print(f"Using existing manifest {path} ({len(load_set.alert_ids)} alerts)")
    else:
        load_set = build_load_set(url, name, a.count, a.seed, eval_ids)
        load_set.save(path)
        print(f"Wrote {len(load_set.alert_ids)} alerts to {path}")
    print(
        f"Excluded {len(eval_ids)} eval-set alerts; first ids: {load_set.alert_ids[:5]}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

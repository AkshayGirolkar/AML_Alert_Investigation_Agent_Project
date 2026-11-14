"""Run the F04 rules and label the resulting alerts."""

from collections.abc import Sequence

import psycopg

from app.config import get_settings
from app.db.copy import conninfo, log
from app.rules.config import ALL_RULE_IDS, RuleConfig
from app.rules.labeling import label_alerts
from app.rules.rules import RULES


def run_rules(
    database_url: str,
    config: RuleConfig | None = None,
    rule_ids: Sequence[str] | None = None,
    reset: bool = False,
) -> dict[str, int]:
    """Insert alerts for the selected rules; return alerts newly inserted per rule.

    Idempotent: re-running inserts nothing new. `reset=True` first deletes the selected
    rules' alerts (needed after changing thresholds, otherwise stale alerts remain).
    """
    cfg = config or RuleConfig.from_settings(get_settings())
    ids = list(rule_ids) if rule_ids is not None else list(ALL_RULE_IDS)
    unknown = [r for r in ids if r not in RULES]
    if unknown:
        raise ValueError(f"unknown rule id(s): {', '.join(unknown)}")

    inserted: dict[str, int] = {}
    with psycopg.connect(conninfo(database_url)) as conn, conn.cursor() as cur:
        if reset:
            cur.execute("DELETE FROM alerts WHERE rule_id = ANY(%s)", (ids,))
            log(f"reset: deleted {cur.rowcount} alerts")
        for rid in ids:
            inserted[rid] = RULES[rid](cur, cfg)
            log(f"{rid}: {inserted[rid]} new alerts")
        label_alerts(cur, ids)
        conn.commit()
    return inserted

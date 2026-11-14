"""Link alerts to ground truth: `is_true_positive` and the attempt typology."""

from collections import Counter
from collections.abc import Iterable

import psycopg


def pick_typology(typologies: Iterable[str | None]) -> str | None:
    """Most frequent typology among the trigger transactions; ties go alphabetically."""
    counts = Counter(t for t in typologies if t)
    if not counts:
        return None
    return min(counts, key=lambda t: (-counts[t], t))


def label_alerts(cur: psycopg.Cursor, rule_ids: list[str]) -> int:
    """(Re)label all alerts of the given rules. Deterministic, safe to repeat."""
    cur.execute(
        """
        SELECT a.alert_id,
               EXISTS (SELECT 1 FROM transactions t
                       WHERE t.txn_id = ANY(a.trigger_txn_ids) AND t.is_laundering),
               ARRAY(SELECT la.typology
                     FROM laundering_attempt_transactions lat
                     JOIN laundering_attempts la USING (attempt_id)
                     WHERE lat.txn_id = ANY(a.trigger_txn_ids))
        FROM alerts a WHERE a.rule_id = ANY(%s)""",
        (rule_ids,),
    )
    rows = [(tp, pick_typology(typs), aid) for aid, tp, typs in cur.fetchall()]
    cur.executemany(
        "UPDATE alerts SET is_true_positive = %s, typology = %s WHERE alert_id = %s",
        rows,
    )
    return len(rows)

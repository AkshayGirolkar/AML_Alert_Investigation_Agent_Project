"""Rules-only baseline: precision and recall per rule and per typology."""

from dataclasses import dataclass

import psycopg

from app.db.copy import conninfo
from app.rules.config import ALL_RULE_IDS

UNTYPED = "untyped"


@dataclass(frozen=True)
class Metrics:
    alerts: int
    true_positive_alerts: int
    precision: float | None  # None when there are no alerts
    fp_rate: float | None  # false-positive alerts / alerts
    recall: float | None  # None when there is no ground truth


@dataclass(frozen=True)
class BaselineReport:
    overall: Metrics
    by_rule: dict[str, Metrics]
    by_typology: dict[str, Metrics]
    attempt_recall: float | None  # share of laundering attempts with >= 1 covered txn


def compute_metrics(
    alerts: int, true_positive_alerts: int, covered: int, total: int
) -> Metrics:
    if true_positive_alerts > alerts or covered > total:
        raise ValueError("impossible counts")
    return Metrics(
        alerts=alerts,
        true_positive_alerts=true_positive_alerts,
        precision=true_positive_alerts / alerts if alerts else None,
        fp_rate=(alerts - true_positive_alerts) / alerts if alerts else None,
        recall=covered / total if total else None,
    )


def compute_baseline(database_url: str) -> BaselineReport:
    with psycopg.connect(conninfo(database_url)) as conn, conn.cursor() as cur:
        cur.execute(
            "CREATE TEMP TABLE _covered ON COMMIT DROP AS "
            "SELECT DISTINCT unnest(trigger_txn_ids) AS txn_id FROM alerts"
        )
        cur.execute("SELECT count(*) FROM transactions WHERE is_laundering")
        total = _one(cur)
        cur.execute(
            "SELECT count(*) FROM transactions t JOIN _covered c USING (txn_id) "
            "WHERE t.is_laundering"
        )
        covered = _one(cur)
        cur.execute(
            "SELECT count(*), count(*) FILTER (WHERE is_true_positive) FROM alerts"
        )
        n_alerts, n_tp = cur.fetchone() or (0, 0)
        overall = compute_metrics(n_alerts, n_tp, covered, total)

        by_rule: dict[str, Metrics] = {}
        for rid in ALL_RULE_IDS:
            cur.execute(
                "SELECT count(*), count(*) FILTER (WHERE is_true_positive) "
                "FROM alerts WHERE rule_id = %s",
                (rid,),
            )
            a, tp = cur.fetchone() or (0, 0)
            cur.execute(
                "SELECT count(DISTINCT t.txn_id) FROM alerts al "
                "JOIN transactions t ON t.txn_id = ANY(al.trigger_txn_ids) "
                "WHERE al.rule_id = %s AND t.is_laundering",
                (rid,),
            )
            by_rule[rid] = compute_metrics(a, tp, _one(cur), total)

        # Per typology: laundering txns grouped by the attempt typology; txns in no
        # attempt form the "untyped" bucket.
        cur.execute(
            "SELECT coalesce(la.typology, %s), count(DISTINCT t.txn_id), "
            "count(DISTINCT t.txn_id) FILTER (WHERE c.txn_id IS NOT NULL) "
            "FROM transactions t "
            "LEFT JOIN laundering_attempt_transactions lat ON lat.txn_id = t.txn_id "
            "LEFT JOIN laundering_attempts la ON la.attempt_id = lat.attempt_id "
            "LEFT JOIN _covered c ON c.txn_id = t.txn_id "
            "WHERE t.is_laundering GROUP BY 1",
            (UNTYPED,),
        )
        truth = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
        cur.execute(
            "SELECT coalesce(typology, %s), count(*), "
            "count(*) FILTER (WHERE is_true_positive) FROM alerts GROUP BY 1",
            (UNTYPED,),
        )
        alert_counts = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
        by_typology: dict[str, Metrics] = {}
        for typ in sorted(set(truth) | set(alert_counts) | {UNTYPED}):
            tot, cov = truth.get(typ, (0, 0))
            a, tp = alert_counts.get(typ, (0, 0))
            by_typology[typ] = compute_metrics(a, tp, cov, tot)

        cur.execute("SELECT count(*) FROM laundering_attempts")
        n_attempts = _one(cur)
        cur.execute(
            "SELECT count(DISTINCT lat.attempt_id) FROM laundering_attempt_transactions "
            "lat JOIN _covered c USING (txn_id)"
        )
        attempt_recall = _one(cur) / n_attempts if n_attempts else None
        conn.commit()
    return BaselineReport(overall, by_rule, by_typology, attempt_recall)


def _one(cur: psycopg.Cursor) -> int:
    row = cur.fetchone()
    return int(row[0]) if row else 0


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.1%}"


def format_report(r: BaselineReport) -> str:
    def row(name: str, m: Metrics) -> str:
        return (
            f"{name:<26}{m.alerts:>9}{m.true_positive_alerts:>9}"
            f"{_pct(m.precision):>11}{_pct(m.fp_rate):>9}{_pct(m.recall):>9}"
        )

    head = f"{'':<26}{'alerts':>9}{'true pos':>9}{'precision':>11}{'FP rate':>9}{'recall':>9}"
    lines = ["Rules-only baseline", "", "By rule", head]
    lines += [row(k, v) for k, v in r.by_rule.items()]
    lines += ["", "By typology (recall = laundering txns covered by any alert)", head]
    lines += [row(k, v) for k, v in r.by_typology.items()]
    lines += ["", row("OVERALL", r.overall)]
    lines.append(f"Attempt-level recall: {_pct(r.attempt_recall)}")
    return "\n".join(lines)

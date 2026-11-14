"""The F04 detection rules: one SQL statement per rule, inserting into `alerts`.

Every rule is idempotent (`ON CONFLICT DO NOTHING` on rule + account + window) and
returns the number of alerts newly inserted. Windows are tumbling buckets aligned to
the epoch. Scores are a deterministic function of the evidence in [0, 1].
"""

from collections.abc import Callable

import psycopg

from app.rules.config import RuleConfig

RuleFn = Callable[[psycopg.Cursor, RuleConfig], int]


def _bucket(col: str, hours: int) -> str:
    """SQL for the start of the tumbling window containing `col`.

    `hours` is a validated int (never user text), so inlining it is safe.
    """
    secs = int(hours) * 3600
    return (
        f"(timestamp 'epoch' + floor(extract(epoch from {col}) / {secs}) * {secs} "
        "* interval '1 second')"
    )


_INSERT = (
    "INSERT INTO alerts (rule_id, account_id, window_start, window_end, "
    "trigger_txn_ids, score) "
)


def high_velocity(cur: psycopg.Cursor, cfg: RuleConfig) -> int:
    ws = _bucket("ts", cfg.velocity_window_hours)
    cur.execute(
        _INSERT
        + f"""
        SELECT 'high_velocity', acct, ws, ws + make_interval(hours => %(w)s),
               ids, least(1, n::numeric / (2 * %(m)s))
        FROM (SELECT from_account_id AS acct, {ws} AS ws,
                     array_agg(txn_id ORDER BY txn_id) AS ids, count(*) AS n
              FROM transactions
              GROUP BY 1, 2
              HAVING count(*) >= %(m)s) b
        ON CONFLICT DO NOTHING""",
        {"w": cfg.velocity_window_hours, "m": cfg.velocity_min_txns},
    )
    return cur.rowcount


def _fan(cur: psycopg.Cursor, cfg: RuleConfig, rule: str, own: str, other: str) -> int:
    ws = _bucket("ts", cfg.fan_window_hours)
    cur.execute(
        _INSERT
        + f"""
        SELECT %(rule)s, acct, ws, ws + make_interval(hours => %(w)s),
               ids, least(1, deg::numeric / (2 * %(m)s))
        FROM (SELECT {own} AS acct, {ws} AS ws,
                     array_agg(txn_id ORDER BY txn_id) AS ids,
                     count(DISTINCT {other}) AS deg
              FROM transactions
              WHERE from_account_id <> to_account_id
              GROUP BY 1, 2
              HAVING count(DISTINCT {other}) >= %(m)s) b
        ON CONFLICT DO NOTHING""",
        {"rule": rule, "w": cfg.fan_window_hours, "m": cfg.fan_min_degree},
    )
    return cur.rowcount


def fan_out(cur: psycopg.Cursor, cfg: RuleConfig) -> int:
    return _fan(cur, cfg, "fan_out", "from_account_id", "to_account_id")


def fan_in(cur: psycopg.Cursor, cfg: RuleConfig) -> int:
    return _fan(cur, cfg, "fan_in", "to_account_id", "from_account_id")


def round_trip(cur: psycopg.Cursor, cfg: RuleConfig) -> int:
    """Time-ordered cycles of 2 (or 3) distinct accounts that close within the window.

    Every account on a cycle is flagged, in the window of the cycle's first transaction.
    """
    win = "make_interval(hours => %(w)s)"
    three = ""
    if cfg.round_trip_max_hops >= 3:
        three = f"""
        UNION ALL
        SELECT t1.ts, ARRAY[t1.from_account_id, t1.to_account_id, t2.to_account_id],
               ARRAY[t1.txn_id, t2.txn_id, t3.txn_id]
        FROM transactions t1
        JOIN transactions t2 ON t2.from_account_id = t1.to_account_id
             AND t2.ts >= t1.ts AND t2.ts <= t1.ts + {win}
        JOIN transactions t3 ON t3.from_account_id = t2.to_account_id
             AND t3.to_account_id = t1.from_account_id
             AND t3.ts >= t2.ts AND t3.ts <= t1.ts + {win}
        WHERE t1.from_account_id <> t1.to_account_id
          AND t2.to_account_id NOT IN (t1.from_account_id, t1.to_account_id)"""
    ws = _bucket("ts1", cfg.round_trip_window_hours)
    cur.execute(
        f"""
        WITH cyc AS (
          SELECT t1.ts AS ts1, ARRAY[t1.from_account_id, t1.to_account_id] AS accts,
                 ARRAY[t1.txn_id, t2.txn_id] AS ids
          FROM transactions t1
          JOIN transactions t2 ON t2.from_account_id = t1.to_account_id
               AND t2.to_account_id = t1.from_account_id
               AND t2.ts >= t1.ts AND t2.ts <= t1.ts + {win}
          WHERE t1.from_account_id <> t1.to_account_id
          {three}
        ),
        flat AS (
          SELECT DISTINCT a AS acct, {ws} AS ws, i AS txn_id
          FROM cyc, unnest(accts) AS a, unnest(ids) AS i
        )
        {_INSERT}
        SELECT 'round_trip', acct, ws, ws + make_interval(hours => %(w)s),
               array_agg(txn_id ORDER BY txn_id), 0.7
        FROM flat GROUP BY acct, ws
        ON CONFLICT DO NOTHING""",
        {"w": cfg.round_trip_window_hours},
    )
    return cur.rowcount


def structuring(cur: psycopg.Cursor, cfg: RuleConfig) -> int:
    """Several USD payments just under the reporting limit (no FX, so USD only)."""
    ws = _bucket("ts", cfg.structuring_window_hours)
    lower = cfg.structuring_limit * (1 - cfg.structuring_margin)
    cur.execute(
        _INSERT
        + f"""
        SELECT 'structuring', acct, ws, ws + make_interval(hours => %(w)s),
               ids, least(1, n::numeric / (2 * %(m)s))
        FROM (SELECT from_account_id AS acct, {ws} AS ws,
                     array_agg(txn_id ORDER BY txn_id) AS ids, count(*) AS n
              FROM transactions
              WHERE payment_currency = 'US Dollar'
                AND amount_paid >= %(lo)s AND amount_paid < %(hi)s
              GROUP BY 1, 2
              HAVING count(*) >= %(m)s) b
        ON CONFLICT DO NOTHING""",
        {
            "w": cfg.structuring_window_hours,
            "m": cfg.structuring_min_txns,
            "lo": lower,
            "hi": cfg.structuring_limit,
        },
    )
    return cur.rowcount


def high_risk_counterparty(cur: psycopg.Cursor, cfg: RuleConfig) -> int:
    """First contact (in the window) with a high-risk or sanctions-matching counterparty.

    Flags the account on the other side of the risky account, in both directions.
    """
    cur.execute("SELECT set_limit(%s::real)", (cfg.sanctions_similarity,))
    cur.execute("DROP TABLE IF EXISTS _risky_accounts")
    cur.execute(
        "CREATE TEMP TABLE _risky_accounts (account_id integer PRIMARY KEY) "
        "ON COMMIT DROP"
    )
    cur.execute(
        """
        INSERT INTO _risky_accounts
        SELECT a.account_id
        FROM accounts a JOIN kyc_profiles k USING (entity_id)
        WHERE k.country = ANY(%(countries)s)
           OR k.risk_rating = ANY(%(ratings)s)
           OR EXISTS (SELECT 1 FROM sanctions_entities s WHERE s.name %% k.full_name)""",
        {
            "countries": list(cfg.high_risk_countries),
            "ratings": list(cfg.high_risk_ratings),
        },
    )
    ws = _bucket("h.ts", cfg.high_risk_window_hours)
    cur.execute(
        _INSERT
        + f"""
        SELECT 'high_risk_counterparty', acct, ws, ws + make_interval(hours => %(w)s),
               array_agg(DISTINCT txn_id ORDER BY txn_id),
               least(1, 0.5 + count(DISTINCT txn_id)::numeric / 10)
        FROM (
          SELECT h.acct, h.cp, h.txn_id, {ws} AS ws, h.ts
          FROM (
            SELECT t.from_account_id AS acct, t.to_account_id AS cp, t.txn_id, t.ts
            FROM transactions t JOIN _risky_accounts r ON r.account_id = t.to_account_id
            UNION ALL
            SELECT t.to_account_id, t.from_account_id, t.txn_id, t.ts
            FROM transactions t JOIN _risky_accounts r ON r.account_id = t.from_account_id
          ) h
          WHERE h.acct <> h.cp
        ) x
        WHERE NOT EXISTS (
          SELECT 1 FROM transactions p
          WHERE p.ts < x.ws
            AND ((p.from_account_id = x.acct AND p.to_account_id = x.cp)
              OR (p.from_account_id = x.cp AND p.to_account_id = x.acct)))
        GROUP BY acct, ws
        ON CONFLICT DO NOTHING""",
        {"w": cfg.high_risk_window_hours},
    )
    inserted = cur.rowcount
    cur.execute("DROP TABLE _risky_accounts")
    return inserted


RULES: dict[str, RuleFn] = {
    "high_velocity": high_velocity,
    "fan_out": fan_out,
    "fan_in": fan_in,
    "round_trip": round_trip,
    "structuring": structuring,
    "high_risk_counterparty": high_risk_counterparty,
}

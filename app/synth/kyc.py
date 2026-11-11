"""Synthetic KYC profiles (F03). Pure generation plus one DB writer."""

from dataclasses import dataclass
from decimal import Decimal

import psycopg

from app.db.copy import conninfo, log
from app.synth.rng import entity_rngs

ELIGIBLE_TYPES = ("Individual", "Corporation", "Partnership", "Sole Proprietorship")
RISK_LEVELS = ("low", "medium", "high")

# Illustrative, not an official list.
HIGH_RISK_COUNTRIES = frozenset({"IR", "KP", "SY", "MM", "AF", "YE", "VE", "CU"})
MEDIUM_RISK_COUNTRIES = frozenset({"RU", "BY", "PK", "NG", "PA", "AE", "TR", "LB"})
_COUNTRIES = sorted(
    HIGH_RISK_COUNTRIES
    | MEDIUM_RISK_COUNTRIES
    | {
        "US",
        "GB",
        "DE",
        "FR",
        "CA",
        "AU",
        "JP",
        "SG",
        "CH",
        "NL",
        "ES",
        "IT",
        "IN",
        "BR",
        "MX",
    }
)
_COUNTRY_WEIGHTS = [
    1 if c in HIGH_RISK_COUNTRIES else 2 if c in MEDIUM_RISK_COUNTRIES else 8
    for c in _COUNTRIES
]
_CASH_BUSINESS_TYPES = ("Sole Proprietorship", "Partnership")
_FALLBACK_VOLUME = (Decimal(1_000), Decimal(50_000))
_MULTIPLIER = (0.5, 2.0)


@dataclass(frozen=True)
class KycProfile:
    entity_id: str
    full_name: str
    occupation: str
    country: str
    expected_monthly_volume: Decimal
    risk_rating: str


def _risk(country: str, entity_type: str, roll: float) -> str:
    score = 0
    score += (
        2
        if country in HIGH_RISK_COUNTRIES
        else 1
        if country in MEDIUM_RISK_COUNTRIES
        else 0
    )
    score += 1 if entity_type in _CASH_BUSINESS_TYPES else 0
    score += (
        1 if roll < 0.15 else 0
    )  # a little noise so ratings are not purely rule-bound
    return "high" if score >= 3 else "medium" if score >= 1 else "low"


def build_profile(
    seed: int, entity_id: str, entity_type: str, avg_monthly_out: Decimal | None
) -> KycProfile:
    rng, fake = entity_rngs(seed, entity_id)
    if entity_type == "Individual":
        name, occupation = fake.name(), fake.job()
    else:
        name, occupation = fake.company(), f"{fake.bs().split()[-1].title()} business"
    country = rng.choices(_COUNTRIES, weights=_COUNTRY_WEIGHTS)[0]
    mult = Decimal(str(round(rng.uniform(*_MULTIPLIER), 3)))
    if avg_monthly_out is None or avg_monthly_out <= 0:
        lo, hi = _FALLBACK_VOLUME
        base = Decimal(rng.randint(int(lo), int(hi)))
        volume = base
    else:
        volume = avg_monthly_out * mult
    volume = max(volume.quantize(Decimal("0.01")), Decimal("0.01"))
    return KycProfile(
        entity_id=entity_id,
        full_name=name,
        occupation=occupation,
        country=country,
        expected_monthly_volume=volume,
        risk_rating=_risk(country, entity_type, rng.random()),
    )


_ENTITY_SQL = """
WITH span AS (
    SELECT greatest(1.0, extract(epoch FROM max(ts) - min(ts)) / 86400.0 / 30.0)
           AS months
    FROM transactions
), out_amt AS (
    SELECT a.entity_id, sum(t.amount_paid) AS total
    FROM transactions t JOIN accounts a ON a.account_id = t.from_account_id
    GROUP BY a.entity_id
)
SELECT e.entity_id, e.entity_type,
       round(o.total / (SELECT months FROM span), 2) AS avg_monthly_out
FROM entities e LEFT JOIN out_amt o USING (entity_id)
WHERE e.entity_type = ANY(%s)
ORDER BY e.entity_id
"""


def generate_kyc(database_url: str, seed: int) -> int:
    """Truncate and reload kyc_profiles (and planted_matches, which depend on it)."""
    with psycopg.connect(conninfo(database_url)) as conn, conn.cursor() as cur:
        rows = cur.execute(_ENTITY_SQL, (list(ELIGIBLE_TYPES),)).fetchall()
        profiles = [build_profile(seed, e, t, v) for e, t, v in rows]
        cur.execute("TRUNCATE planted_matches, kyc_profiles")
        cur.executemany(
            "INSERT INTO kyc_profiles (entity_id, full_name, occupation, country, "
            "expected_monthly_volume, risk_rating) VALUES (%s,%s,%s,%s,%s,%s)",
            [
                (
                    p.entity_id,
                    p.full_name,
                    p.occupation,
                    p.country,
                    p.expected_monthly_volume,
                    p.risk_rating,
                )
                for p in profiles
            ],
        )
        conn.commit()
    log(f"kyc_profiles: {len(profiles):,} rows (seed={seed})")
    return len(profiles)

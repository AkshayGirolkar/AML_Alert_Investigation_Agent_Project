"""Plant a few sanctioned names into KYC profiles (the F15 answer key)."""

import random
import re
from dataclasses import dataclass

import psycopg

from app.db.copy import conninfo, log
from app.synth.kyc import generate_kyc
from app.synth.rng import entity_seed

VARIANT_KINDS = ("exact", "reordered", "typo")
_PAIRS = {
    "Individual": frozenset({"Person"}),
    "Corporation": frozenset({"Company", "Organization", "LegalEntity"}),
}
_ASCII_NAME = re.compile(r"^[A-Za-z][A-Za-z .'\-]*$")
_MIN_LEN = 8  # shorter names drop below the trigram threshold after a typo
_LETTERS = "abcdefghijklmnopqrstuvwxyz"


@dataclass(frozen=True)
class Plant:
    entity_id: str
    source_id: str
    variant_kind: str
    planted_name: str


def reorder_name(name: str, rng: random.Random) -> str:
    words = name.split()
    if len(set(words)) < 2:
        raise ValueError(f"cannot reorder {name!r}")
    for _ in range(50):
        shuffled = words[:]
        rng.shuffle(shuffled)
        if shuffled != words:
            return " ".join(shuffled)
    raise ValueError(f"cannot reorder {name!r}")  # pragma: no cover


def typo_name(name: str, rng: random.Random) -> str:
    """Substitute one letter (never the first character) for a different one."""
    idx = [i for i, ch in enumerate(name) if i > 0 and ch.isalpha()]
    if not idx:
        raise ValueError(f"cannot add a typo to {name!r}")
    i = rng.choice(idx)
    ch = name[i]
    new = rng.choice([c for c in _LETTERS if c != ch.lower()])
    return name[:i] + (new.upper() if ch.isupper() else new) + name[i + 1 :]


def _variant(kind: str, name: str, rng: random.Random) -> str | None:
    try:
        if kind == "exact":
            return name
        if kind == "reordered":
            return reorder_name(name, rng)
        return typo_name(name, rng)
    except ValueError:
        return None


def choose_plants(
    seed: int,
    entities: list[tuple[str, str]],
    sanctions: list[tuple[str, str, str]],
    n: int = 15,
) -> list[Plant]:
    """Pick up to n (entity, sanctions row, variant) triples, independent of input order."""
    rng = random.Random(seed)
    cands = sorted(
        (e for e in entities if e[1] in _PAIRS),
        key=lambda e: entity_seed(seed, e[0], "plant"),
    )
    pool = sorted(
        s for s in sanctions if _ASCII_NAME.match(s[1]) and len(s[1]) >= _MIN_LEN
    )
    used: set[str] = set()
    plants: list[Plant] = []
    for entity_id, etype in cands:
        if len(plants) >= n:
            break
        kind = VARIANT_KINDS[len(plants) % 3]
        options = [s for s in pool if s[2] in _PAIRS[etype] and s[0] not in used]
        rng.shuffle(options)
        for src_id, name, _ in options:
            planted = _variant(kind, name, rng)
            if planted is not None:
                used.add(src_id)
                plants.append(Plant(entity_id, src_id, kind, planted))
                break
    return plants


def plant_matches(database_url: str, seed: int, n: int = 15) -> int:
    """Regenerate KYC (restores non-planted names), then plant. Idempotent."""
    generate_kyc(database_url, seed)
    with psycopg.connect(conninfo(database_url)) as conn, conn.cursor() as cur:
        ents = cur.execute(
            "SELECT entity_id, entity_type FROM entities "
            "WHERE entity_id IN (SELECT entity_id FROM kyc_profiles)"
        ).fetchall()
        sanc = cur.execute(
            "SELECT source_id, name, schema_type FROM sanctions_entities"
        ).fetchall()
        plants = choose_plants(seed, ents, sanc, n)
        for p in plants:
            cur.execute(
                "UPDATE kyc_profiles SET full_name = %s WHERE entity_id = %s",
                (p.planted_name, p.entity_id),
            )
        cur.executemany(
            "INSERT INTO planted_matches (entity_id, sanctions_source_id, variant_kind) "
            "VALUES (%s, %s, %s)",
            [(p.entity_id, p.source_id, p.variant_kind) for p in plants],
        )
        conn.commit()
    log(f"planted_matches: {len(plants)} rows")
    return len(plants)

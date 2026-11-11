"""Per-entity deterministic randomness: results never depend on row order."""

import hashlib
import random

from faker import Faker

_FAKE = Faker("en_US")


def entity_seed(seed: int, entity_id: str, salt: str = "") -> int:
    digest = hashlib.md5(f"{seed}|{salt}|{entity_id}".encode()).hexdigest()
    return int(digest[:8], 16)


def entity_rngs(
    seed: int, entity_id: str, salt: str = ""
) -> tuple[random.Random, Faker]:
    s = entity_seed(seed, entity_id, salt)
    # Building a Faker is slow (~ms), so one instance is reseeded per entity.
    # Not thread-safe: callers must finish with the returned Faker before the
    # next entity_rngs call.
    fake = _FAKE
    fake.seed_instance(s)
    return random.Random(s), fake

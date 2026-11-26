"""Typology names: the IBM patterns vocabulary and a normaliser for agent output (F15)."""

import re

IBM_TYPOLOGIES: tuple[str, ...] = (
    "BIPARTITE",
    "CYCLE",
    "FAN-IN",
    "FAN-OUT",
    "GATHER-SCATTER",
    "RANDOM",
    "SCATTER-GATHER",
    "STACK",
)

UNTYPED = "UNTYPED"  # true alerts whose trigger transactions have no attempt typology

# Extra spellings the agent might use, after upper-casing and turning separators into "-".
_ALIASES: dict[str, str] = {
    "FANIN": "FAN-IN",
    "FANOUT": "FAN-OUT",
    "GATHERSCATTER": "GATHER-SCATTER",
    "SCATTERGATHER": "SCATTER-GATHER",
    "ROUND-TRIP": "CYCLE",
    "ROUND-TRIPPING": "CYCLE",
    "ROUNDTRIP": "CYCLE",
    "CIRCULAR": "CYCLE",
    "CYCLES": "CYCLE",
    "STACKED": "STACK",
    "STACKING": "STACK",
    "BIPARTITE-GRAPH": "BIPARTITE",
}

_SEPARATORS = re.compile(r"[\s_\-/]+")


def normalize_typology(name: str | None) -> str | None:
    """Map a free-text typology to an IBM name, or None if it is empty or unknown.

    Case, spaces, underscores and hyphens are ignored ("fan in" == "FAN-IN").
    """
    if not name or not name.strip():
        return None
    key = _SEPARATORS.sub("-", name.strip().upper()).strip("-")
    if key in IBM_TYPOLOGIES:
        return key
    return _ALIASES.get(key) or _ALIASES.get(key.replace("-", ""))

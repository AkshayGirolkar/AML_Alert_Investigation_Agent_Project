"""F16: migration 0010 keeps `sanctions_aliases` in sync and matches the old scan."""

import pytest

from tests.integration.test_rules import _conn, _db, _q  # noqa: F401

ROWS = [
    ("A-1", "Ivan Sidorov", ["Viktor Petrow", "Ivan S. Sidorov"]),
    ("A-2", "Acme Trading Ltd", ["Acme Trade Limited", "ACME TRADING"]),
    ("A-3", "Maria Gonzalez", []),
]


@pytest.fixture
def entities(_db: str) -> str:  # noqa: F811
    with _conn(_db) as c:
        c.execute("TRUNCATE planted_matches, sanctions_entities")
        for sid, name, aliases in ROWS:
            c.execute(
                "INSERT INTO sanctions_entities (source_id, name, aliases, schema_type, "
                "countries, dataset) VALUES (%s, %s, %s, 'Person', '{}', '{}')",
                (sid, name, aliases),
            )
    return _db


def _aliases(url: str) -> set[tuple[str, str]]:
    return {
        (a, b) for a, b in _q(url, "SELECT source_id, alias FROM sanctions_aliases")
    }


def test_insert_populates_aliases(entities: str) -> None:
    assert _aliases(entities) == {
        ("A-1", "Viktor Petrow"),
        ("A-1", "Ivan S. Sidorov"),
        ("A-2", "Acme Trade Limited"),
        ("A-2", "ACME TRADING"),
    }


def test_update_and_delete_follow(entities: str) -> None:
    with _conn(entities) as c:
        c.execute(
            "UPDATE sanctions_entities SET aliases = '{New Alias}' WHERE source_id = 'A-1'"
        )
        c.execute("DELETE FROM sanctions_entities WHERE source_id = 'A-2'")
    assert _aliases(entities) == {("A-1", "New Alias")}


def test_truncate_clears_aliases(entities: str) -> None:
    with _conn(entities) as c:
        c.execute("TRUNCATE planted_matches, sanctions_entities")
    assert _aliases(entities) == set()


@pytest.mark.parametrize("threshold", [0.3, 0.5, 0.8])
def test_indexed_search_equals_the_old_scan(entities: str, threshold: float) -> None:
    old = """SELECT s.source_id, al.alias FROM sanctions_entities s
             CROSS JOIN LATERAL unnest(s.aliases) AS al(alias)
             WHERE similarity(al.alias, %(q)s) >= %(t)s"""
    new = """SELECT a.source_id, a.alias FROM sanctions_aliases a
             WHERE a.alias %% %(q)s AND similarity(a.alias, %(q)s) >= %(t)s"""
    for q in ("Viktor Petrov", "Acme Trading", "ACME TRADE LTD", "Ivan Sidorov", "zzz"):
        with _conn(entities) as c:
            c.execute("SELECT set_limit(%s::real)", (threshold,))
            a = set(c.execute(old, {"q": q, "t": threshold}).fetchall())
            b = set(c.execute(new, {"q": q, "t": threshold}).fetchall())
        assert a == b, (q, threshold)

"""Integration tests for F05 against the Docker Postgres (`aml_test` database).

Each test builds its own tiny world (reusing the F04 `World` builder). Skipped when
Postgres is not reachable. Embeddings use FakeEmbedder.
"""

import json
from collections.abc import AsyncIterator, Iterator
from datetime import date, datetime, timedelta
from typing import Any

import psycopg
import pytest
from sqlalchemy.engine import make_url

from app.agent.tools.media import adverse_media
from app.agent.tools.network import counterparty_network
from app.agent.tools.peers import peer_comparison
from app.agent.tools.registry import build_tools, make_conn_factory
from app.agent.tools.sanctions import sanctions_screen
from app.agent.tools.transactions import query_transactions
from app.config import get_settings
from app.db.copy import conninfo
from app.services.sanctions_cache import SanctionsCache
from app.synth.embeddings import FakeEmbedder
from tests.integration.test_rules import TEST_DB, World, _alembic, _conn

DAY = datetime(2022, 9, 5)
GROUND_TRUTH_KEYS = {
    "is_laundering",
    "is_decoy",
    "theme",
    "is_true_positive",
    "typology",
    "variant_kind",
    "sanctions_source_id",
}


@pytest.fixture(scope="module")
def _db() -> str:
    base = make_url(get_settings().sync_database_url)
    admin = base.set(drivername="postgresql").render_as_string(hide_password=False)
    try:
        with psycopg.connect(admin, autocommit=True, connect_timeout=3) as conn:
            if not conn.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s", (TEST_DB,)
            ).fetchone():
                conn.execute(f"CREATE DATABASE {TEST_DB}")
    except psycopg.OperationalError:
        pytest.skip("Postgres is not reachable")
    _alembic("upgrade", "head")
    return base.set(database=TEST_DB).render_as_string(hide_password=False)


@pytest.fixture
def w(_db: str) -> Iterator[World]:
    with _conn(_db) as c:
        c.execute(
            "TRUNCATE alerts, adverse_media, planted_matches, sanctions_entities, "
            "laundering_attempt_transactions, laundering_attempts, kyc_profiles, "
            "transactions, accounts, entities, banks RESTART IDENTITY CASCADE"
        )
    yield World(_db)


@pytest.fixture
async def conn(w: World) -> AsyncIterator[psycopg.AsyncConnection]:
    async with await psycopg.AsyncConnection.connect(conninfo(w.url)) as c:
        yield c


def _set_kyc(w: World, account: int, **cols: Any) -> None:
    sets = ", ".join(f"{k} = %s" for k in cols)
    with _conn(w.url) as c:
        c.execute(
            f"UPDATE kyc_profiles SET {sets} WHERE entity_id = "
            "(SELECT entity_id FROM accounts WHERE account_id = %s)",
            (*cols.values(), account),
        )


def _entity(w: World, account: int) -> str:
    with _conn(w.url) as c:
        row = c.execute(
            "SELECT entity_id FROM accounts WHERE account_id = %s", (account,)
        ).fetchone()
    assert row is not None
    return str(row[0])


def _walk_keys(obj: Any) -> Iterator[str]:
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from _walk_keys(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk_keys(v)


# ------------------------------------------------------------ query_transactions


async def test_query_transactions_aggregates_per_currency(
    w: World, conn: psycopg.AsyncConnection
) -> None:
    a, b, c = w.account(), w.account(), w.account()
    w.txn(a, b, DAY, 100)
    w.txn(a, b, DAY + timedelta(hours=1), 200)
    w.txn(c, a, DAY + timedelta(hours=2), 50, currency="Euro")
    ev = await query_transactions(conn, account_id=a)
    assert ev.status == "ok" and ev.evidence_id
    d = ev.data
    assert (d["transaction_count"], d["outgoing_count"], d["incoming_count"]) == (
        3,
        2,
        1,
    )
    assert d["distinct_counterparties"] == 2
    cur = {(r["currency"], r["direction"]): r for r in d["by_currency"]}
    assert cur[("US Dollar", "out")]["total"] == "300"
    assert cur[("US Dollar", "out")]["largest"] == "200"
    assert cur[("Euro", "in")]["total"] == "50"
    assert d["by_format"] == [{"format": "ACH", "txn_count": 3}]
    assert [s["txn_id"] for s in d["sample"]] == [3, 2, 1]  # latest first
    assert "3 transactions (2 out, 1 in)" in ev.summary


async def test_query_transactions_sample_cap_direction_and_window(
    w: World, conn: psycopg.AsyncConnection
) -> None:
    a, b = w.account(), w.account()
    for i in range(12):
        w.txn(a, b, DAY + timedelta(hours=i), 10)
    assert (
        len(
            (await query_transactions(conn, account_id=a, sample_size=4)).data["sample"]
        )
        == 4
    )
    assert (
        await query_transactions(conn, account_id=a, direction="in")
    ).status == "empty"
    out = await query_transactions(
        conn, account_id=a, start=DAY + timedelta(hours=10), end=DAY + timedelta(days=1)
    )
    assert out.data["transaction_count"] == 2  # hours 10 and 11
    none = await query_transactions(conn, account_id=a, start=DAY + timedelta(days=5))
    assert none.status == "empty" and none.evidence_id
    assert none.data["transaction_count"] == 0


async def test_query_transactions_unknown_account_is_empty_not_error(
    w: World, conn: psycopg.AsyncConnection
) -> None:
    ev = await query_transactions(conn, account_id=999)
    assert ev.status == "empty" and ev.data["account_exists"] is False


async def test_same_call_gives_same_evidence_id_and_data(
    w: World, conn: psycopg.AsyncConnection
) -> None:
    a, b = w.account(), w.account()
    w.txn(a, b, DAY)
    one = await query_transactions(conn, account_id=a)
    two = await query_transactions(conn, account_id=a)
    assert one.evidence_id == two.evidence_id and one.data == two.data
    other = await query_transactions(conn, account_id=a, direction="out")
    assert other.evidence_id != one.evidence_id


# ---------------------------------------------------------- counterparty_network


async def test_network_hops_and_labels(w: World, conn: psycopg.AsyncConnection) -> None:
    a, b, c, d = (w.account() for _ in range(4))
    w.txn(a, b, DAY, 100)
    w.txn(b, c, DAY, 90)
    w.txn(c, d, DAY, 80)
    one = await counterparty_network(conn, account_id=a, hops=1)
    assert {n["account_id"]: n["hop"] for n in one.data["nodes"]} == {a: 0, b: 1}
    two = await counterparty_network(conn, account_id=a, hops=2)
    assert {n["account_id"]: n["hop"] for n in two.data["nodes"]} == {a: 0, b: 1, c: 2}
    edges = {(e["from"], e["to"]): e["total"] for e in two.data["edges"]}
    assert edges == {(a, b): "100", (b, c): "90"}  # c->d is outside 2 hops


async def test_network_finds_round_trip_cycles(
    w: World, conn: psycopg.AsyncConnection
) -> None:
    a, b, c = w.account(), w.account(), w.account()
    w.txn(a, b, DAY)
    w.txn(b, c, DAY)
    w.txn(c, a, DAY)
    cycles = (await counterparty_network(conn, account_id=a)).data[
        "cycles_through_account"
    ]
    assert cycles == [[a, b, c]]
    w.txn(b, a, DAY)  # add a 2-cycle too
    cycles = (await counterparty_network(conn, account_id=a)).data[
        "cycles_through_account"
    ]
    assert cycles == [[a, b], [a, b, c]]


async def test_network_hub_is_capped_and_flagged(
    w: World, conn: psycopg.AsyncConnection
) -> None:
    hub = w.account()
    spokes = [w.account() for _ in range(6)]
    for i, s in enumerate(spokes):
        for _ in range(i + 1):  # spoke i has i+1 txns: the busiest ones are kept
            w.txn(hub, s, DAY)
    ev = await counterparty_network(conn, account_id=hub, hops=1, max_neighbors=3)
    kept = {n["account_id"] for n in ev.data["nodes"]} - {hub}
    assert kept == set(spokes[-3:])
    assert ev.data["root_degree"] == 6 and ev.data["truncated"] is True


async def test_network_window_isolated_and_unknown(
    w: World, conn: psycopg.AsyncConnection
) -> None:
    a, b, lone = w.account(), w.account(), w.account()
    w.txn(a, b, DAY)
    assert (await counterparty_network(conn, account_id=lone)).status == "empty"
    late = await counterparty_network(conn, account_id=a, start=DAY + timedelta(days=1))
    assert late.status == "empty" and late.data["root_degree"] == 0
    assert (await counterparty_network(conn, account_id=999)).data[
        "account_exists"
    ] is False


async def test_network_includes_kyc_context_on_nodes(
    w: World, conn: psycopg.AsyncConnection
) -> None:
    a = w.account()
    b = w.account(country="IR", rating="high")
    w.txn(a, b, DAY)
    node = next(
        n
        for n in (await counterparty_network(conn, account_id=a)).data["nodes"]
        if n["account_id"] == b
    )
    assert node["country"] == "IR" and node["risk_rating"] == "high"


# ------------------------------------------------------------- sanctions_screen


def _sanction(
    w: World,
    sid: str,
    name: str,
    aliases: list[str] | None = None,
    countries: str = "ru",
) -> None:
    with _conn(w.url) as c:
        c.execute(
            "INSERT INTO sanctions_entities VALUES (%s, %s, %s, 'Person', %s, %s, NULL)",
            (sid, name, aliases or [], [countries], ["test_list"]),
        )


@pytest.mark.parametrize(
    ("customer", "min_strength"),
    [
        ("Viktor Petrov", "strong"),  # exact
        ("Petrov Viktor", "strong"),  # reordered
        ("Viktor Petrow", "probable"),  # one-character typo
    ],
)
async def test_sanctions_screen_finds_name_variants(
    w: World, conn: psycopg.AsyncConnection, customer: str, min_strength: str
) -> None:
    _sanction(w, "NK-1", "Viktor Petrov")
    _sanction(w, "NK-2", "Completely Different Name")
    a = w.account(country="RU")
    _set_kyc(w, a, full_name=customer)
    ev = await sanctions_screen(conn, account_id=a)
    assert ev.status == "ok" and ev.evidence_id
    top = ev.data["matches"][0]
    assert top["source_id"] == "NK-1" and top["matched_on"] == "name"
    assert top["country_match"] is True
    order = ["weak", "probable", "strong"]
    assert order.index(top["strength"]) >= order.index(min_strength)
    assert all(m["source_id"] != "NK-2" for m in ev.data["matches"])


async def test_sanctions_screen_cache_matches_uncached_and_serves_repeats(
    w: World, conn: psycopg.AsyncConnection, real_redis: Any
) -> None:
    _sanction(w, "NK-1", "Viktor Petrov", countries="ru")
    _sanction(w, "NK-3", "Ivan Sidorov", aliases=["Viktor Petrow"])
    a = w.account(country="RU")
    _set_kyc(w, a, full_name="Viktor Petrov")
    cache = SanctionsCache(real_redis, 60)

    plain = await sanctions_screen(conn, account_id=a)
    miss = await sanctions_screen(conn, cache, account_id=a)
    assert miss.data == plain.data and miss.summary == plain.summary
    assert len([k async for k in real_redis.scan_iter("sanctions:0:*")]) == 1

    # Prove the repeat comes from Redis: the list is emptied, the answer does not change.
    with _conn(w.url) as c:
        c.execute("DELETE FROM sanctions_entities")
    hit = await sanctions_screen(conn, cache, account_id=a)
    assert hit.data == plain.data and hit.status == "ok"
    assert (await sanctions_screen(conn, account_id=a)).status == "empty"

    # `limit` is applied after the cache read, so it still works on a cached entry.
    one = await sanctions_screen(conn, cache, account_id=a, limit=1)
    assert len(one.data["matches"]) == 1


async def test_sanctions_screen_matches_aliases(
    w: World, conn: psycopg.AsyncConnection
) -> None:
    _sanction(w, "NK-3", "Ivan Sidorov", aliases=["Johnny Whiteford"])
    a = w.account()
    _set_kyc(w, a, full_name="Johnny Whiteford")
    top = (await sanctions_screen(conn, account_id=a)).data["matches"][0]
    assert top["source_id"] == "NK-3"
    assert top["matched_on"] == "alias" and top["matched_text"] == "Johnny Whiteford"
    assert top["country_match"] is False  # customer is US, listing is RU


async def test_sanctions_screen_no_match_unknown_and_no_kyc(
    w: World, conn: psycopg.AsyncConnection
) -> None:
    _sanction(w, "NK-1", "Viktor Petrov")
    a = w.account()
    _set_kyc(w, a, full_name="Mary Smith")
    ev = await sanctions_screen(conn, account_id=a)
    assert ev.status == "empty" and ev.data["matches"] == [] and ev.evidence_id
    assert (await sanctions_screen(conn, account_id=999)).data[
        "account_exists"
    ] is False
    with _conn(w.url) as c:  # an account whose entity has no KYC profile
        c.execute("INSERT INTO entities VALUES ('NOKYC', 'Country #1', 'Country')")
        c.execute(
            "INSERT INTO accounts (bank_id, account_number, entity_id) "
            "VALUES (1, 'NOKYC1', 'NOKYC')"
        )
        row = c.execute(
            "SELECT account_id FROM accounts WHERE entity_id='NOKYC'"
        ).fetchone()
    assert row is not None
    nokyc = await sanctions_screen(conn, account_id=row[0])
    assert nokyc.status == "empty" and nokyc.data["has_kyc"] is False


async def test_sanctions_screen_restores_the_trigram_limit(
    w: World, conn: psycopg.AsyncConnection
) -> None:
    _sanction(w, "NK-1", "Viktor Petrov")
    a = w.account()
    _set_kyc(w, a, full_name="Viktor Petrov")
    before = await (await conn.execute("SELECT show_limit()")).fetchone()
    await sanctions_screen(conn, account_id=a, min_similarity=0.9)
    assert await (await conn.execute("SELECT show_limit()")).fetchone() == before


# ----------------------------------------------------------------- adverse_media


def _article(
    w: World, entity: str, headline: str, day: date, decoy: bool = False
) -> None:
    vec = FakeEmbedder._one(headline)
    with _conn(w.url) as c:
        c.execute(
            "INSERT INTO adverse_media (entity_id, headline, body, published_at, "
            "is_decoy, theme, embedding) VALUES (%s,%s,%s,%s,%s,'t',%s::vector)",
            (
                entity,
                headline,
                headline + " " + "x" * 400,
                day,
                decoy,
                "[" + ",".join(map(repr, vec)) + "]",
            ),
        )


async def test_adverse_media_newest_first_without_embedder(
    w: World, conn: psycopg.AsyncConnection
) -> None:
    a, other = w.account(), w.account()
    ea = _entity(w, a)
    _article(w, ea, "Old story", date(2021, 1, 1))
    _article(w, ea, "New story", date(2022, 6, 1))
    _article(w, _entity(w, other), "Someone else", date(2022, 7, 1))
    ev = await adverse_media(conn, account_id=a)
    assert ev.status == "ok" and ev.data["total_articles"] == 2
    assert [x["headline"] for x in ev.data["articles"]] == ["New story", "Old story"]
    assert len(ev.data["articles"][0]["snippet"]) == 300
    assert "is_decoy" not in ev.data["articles"][0]


async def test_adverse_media_semantic_ranking_and_limit(
    w: World, conn: psycopg.AsyncConnection
) -> None:
    a = w.account()
    ea = _entity(w, a)
    for i, h in enumerate(["Bribery probe", "Shell company scheme", "Tax dispute"]):
        _article(w, ea, h, date(2022, 1, 1 + i))
    ev = await adverse_media(
        conn, FakeEmbedder(), account_id=a, query="Shell company scheme", limit=2
    )
    arts = ev.data["articles"]
    assert len(arts) == 2 and arts[0]["headline"] == "Shell company scheme"
    assert arts[0]["similarity"] == pytest.approx(1.0, abs=1e-3)
    assert arts[0]["similarity"] >= arts[1]["similarity"]


async def test_adverse_media_edge_cases(
    w: World, conn: psycopg.AsyncConnection
) -> None:
    a = w.account()
    none = await adverse_media(conn, account_id=a)
    assert none.status == "empty" and none.data["articles"] == []
    no_embedder = await adverse_media(conn, account_id=a, query="anything")
    assert no_embedder.status == "error" and no_embedder.evidence_id is None
    assert (await adverse_media(conn, account_id=999)).data["account_exists"] is False


# ----------------------------------------------------------------- peer_comparison


async def test_peer_comparison_percentile_and_volume_ratio(
    w: World, conn: psycopg.AsyncConnection
) -> None:
    subject = w.account()
    sink = w.account(country="FR")
    peers = [w.account() for _ in range(4)]
    outsider = w.account(country="FR")  # different country: not a peer
    _set_kyc(w, subject, expected_monthly_volume=500)
    for _ in range(5):
        w.txn(subject, sink, DAY, 100)
    for i, p in enumerate(peers):
        for _ in range(i):  # peers make 0..3 txns
            w.txn(p, sink, DAY, 10)
    for _ in range(50):
        w.txn(outsider, sink, DAY, 10)
    ev = await peer_comparison(conn, account_id=subject)
    assert ev.status == "ok"
    assert ev.data["peer_group"]["peer_count"] == 4
    m = ev.data["metrics"]["outgoing_txn_count"]
    assert (m["subject"], m["peer_median"], m["peer_max"], m["percentile"]) == (
        5,
        1.5,
        3,
        1.0,
    )
    assert ev.data["metrics"]["outgoing_total"]["subject"] == "500"
    assert ev.data["outgoing_total_to_expected_monthly_ratio"] == "1"
    assert "100th percentile" in ev.summary


async def test_peer_comparison_window_no_peers_and_no_kyc(
    w: World, conn: psycopg.AsyncConnection
) -> None:
    lone, sink = w.account(country="NZ"), w.account(country="FR")
    w.txn(lone, sink, DAY, 10)
    ev = await peer_comparison(conn, account_id=lone)
    assert ev.status == "ok" and ev.data["peer_group"]["peer_count"] == 0
    assert ev.data["metrics"]["outgoing_txn_count"]["percentile"] is None
    assert "no peers" in ev.summary
    late = await peer_comparison(conn, account_id=lone, start=DAY + timedelta(days=3))
    assert late.data["metrics"]["outgoing_txn_count"]["subject"] == 0
    assert (await peer_comparison(conn, account_id=999)).data["account_exists"] is False
    with _conn(w.url) as c:
        c.execute("INSERT INTO entities VALUES ('NOKYC', 'Direct #1', 'Direct')")
        row = c.execute(
            "INSERT INTO accounts (bank_id, account_number, entity_id) "
            "VALUES (1, 'NOKYC1', 'NOKYC') RETURNING account_id"
        ).fetchone()
    assert row is not None
    assert (await peer_comparison(conn, account_id=row[0])).data["has_kyc"] is False


# ------------------------------------------------------------ cross-cutting checks


async def test_no_tool_exposes_ground_truth(
    w: World, conn: psycopg.AsyncConnection
) -> None:
    a, b = w.account(), w.account()
    t = w.txn(a, b, DAY, 100, laundering=True)
    w.attempt("FAN-OUT", [t])
    _sanction(w, "NK-1", "Viktor Petrov")
    _set_kyc(w, a, full_name="Viktor Petrov")
    with _conn(w.url) as c:
        c.execute(
            "INSERT INTO planted_matches VALUES (%s, 'NK-1', 'exact')", (_entity(w, a),)
        )
    _article(w, _entity(w, a), "Fraud probe", date(2022, 1, 1), decoy=True)
    tools = {x.name: x for x in build_tools(make_conn_factory(w.url), FakeEmbedder())}
    for name, tool in tools.items():
        out = await tool.ainvoke({"account_id": a})
        assert out["status"] in ("ok", "empty"), name
        assert not (set(_walk_keys(out)) & GROUND_TRUTH_KEYS), name
        json.dumps(out)  # JSON-safe (no Decimal/datetime leaking through)
    # and the semantic-search path
    out = await tools["adverse_media"].ainvoke({"account_id": a, "query": "fraud"})
    assert not (set(_walk_keys(out)) & GROUND_TRUTH_KEYS)


async def test_default_connections_are_read_only(w: World) -> None:
    async with make_conn_factory(w.url)() as c:
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            await c.execute("DELETE FROM transactions")

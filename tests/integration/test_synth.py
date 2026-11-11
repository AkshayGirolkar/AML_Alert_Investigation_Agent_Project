"""Integration tests for F03 against the Docker Postgres (`aml_test` database).

Skipped when Postgres is not reachable. Embeddings use a fake client.
"""

import os
from pathlib import Path

import psycopg
import pytest
from alembic.config import Config
from sqlalchemy.engine import make_url

from alembic import command
from app.config import get_settings
from app.ingest.loader import LoadOptions, load
from app.synth.embeddings import DIM, EmbeddingCache, FakeEmbedder
from app.synth.kyc import ELIGIBLE_TYPES, generate_kyc
from app.synth.media import generate_media
from app.synth.planter import plant_matches
from app.synth.sanctions import load_sanctions

ROOT = Path(__file__).parent.parent.parent
RAW = ROOT / "tests" / "fixtures" / "raw"
SANCTIONS_CSV = ROOT / "tests" / "fixtures" / "sanctions" / "targets.simple.csv"
TEST_DB = "aml_test"
EXTRA_ENTITIES = 80


def _alembic_cfg() -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    return cfg


def _alembic(action: str, rev: str) -> None:
    prev = os.environ.get("POSTGRES_DB")
    os.environ["POSTGRES_DB"] = TEST_DB
    try:
        getattr(command, action)(_alembic_cfg(), rev)
    finally:
        if prev is None:
            del os.environ["POSTGRES_DB"]
        else:
            os.environ["POSTGRES_DB"] = prev


def _conn(url: str) -> psycopg.Connection:
    u = make_url(url).set(drivername="postgresql")
    return psycopg.connect(u.render_as_string(hide_password=False), autocommit=True)


def _q(url: str, sql: str) -> list[tuple]:
    with _conn(url) as c:
        return c.execute(sql).fetchall()


@pytest.fixture(scope="module")
def url() -> str:
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
    u = base.set(database=TEST_DB).render_as_string(hide_password=False)
    load(LoadOptions(data_dir=RAW, database_url=u, slice_mode=True, slice_size=100))
    with _conn(u) as c:  # more entities so selection/planting have material
        for i in range(EXTRA_ENTITIES):
            kind = ELIGIBLE_TYPES[i % 4]
            c.execute(
                "INSERT INTO entities VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                (f"X{i}", f"{kind} #9{i}", kind),
            )
        c.execute("INSERT INTO entities VALUES ('XC', 'Country #1', 'Country')")
    load_sanctions(u, SANCTIONS_CSV)
    return u


def _eligible(url: str) -> int:
    return _q(
        url,
        "SELECT count(*) FROM entities WHERE entity_type IN "
        "('Individual','Corporation','Partnership','Sole Proprietorship')",
    )[0][0]


def test_kyc_count_and_idempotent(url: str) -> None:
    n = generate_kyc(url, 42)
    assert n == _eligible(url)
    first = _q(url, "SELECT * FROM kyc_profiles ORDER BY entity_id")
    generate_kyc(url, 42)
    assert _q(url, "SELECT * FROM kyc_profiles ORDER BY entity_id") == first
    assert _q(
        url,
        "SELECT count(*) FROM kyc_profiles k JOIN entities e USING (entity_id) "
        "WHERE e.entity_type IN ('Country','Direct')",
    ) == [(0,)]
    generate_kyc(url, 7)
    assert _q(url, "SELECT * FROM kyc_profiles ORDER BY entity_id") != first


def test_kyc_volume_uses_real_activity(url: str) -> None:
    generate_kyc(url, 42)
    # ENT1 sends transactions in the fixtures; X0 has no accounts at all
    (v_active,) = _q(
        url, "SELECT expected_monthly_volume FROM kyc_profiles WHERE entity_id='ENT1'"
    )[0]
    (v_idle,) = _q(
        url, "SELECT expected_monthly_volume FROM kyc_profiles WHERE entity_id='X0'"
    )[0]
    assert v_active > 0 and v_idle > 0


def test_sanctions_load_filters_and_is_idempotent(url: str) -> None:
    assert load_sanctions(url, SANCTIONS_CSV) == 15  # 20 rows, 5 non-loadable
    assert _q(url, "SELECT count(*) FROM sanctions_entities") == [(15,)]
    assert _q(
        url,
        "SELECT count(*) FROM sanctions_entities WHERE schema_type NOT IN "
        "('Person','Organization','LegalEntity','Company')",
    ) == [(0,)]
    assert _q(
        url, "SELECT aliases, countries FROM sanctions_entities WHERE source_id='P1'"
    ) == [(["V. Petrenko", "Victor Petrenko"], ["ru", "by"])]
    assert _q(url, "SELECT aliases FROM sanctions_entities WHERE source_id='P2'") == [
        ([],)
    ]
    before = _q(url, "SELECT * FROM sanctions_entities ORDER BY source_id")
    load_sanctions(url, SANCTIONS_CSV)
    assert _q(url, "SELECT * FROM sanctions_entities ORDER BY source_id") == before
    assert _q(
        url, "SELECT count(*) FROM pg_tables WHERE tablename LIKE 'stg_sanctions%'"
    ) == [(0,)]


def test_planted_matches_found_by_trigram_and_idempotent(url: str) -> None:
    generate_kyc(url, 42)
    n = plant_matches(url, 42, 15)
    assert n > 0
    rows = _q(
        url,
        """SELECT p.variant_kind, k.full_name, s.name, similarity(k.full_name, s.name),
                  k.full_name % s.name
           FROM planted_matches p
           JOIN kyc_profiles k USING (entity_id)
           JOIN sanctions_entities s ON s.source_id = p.sanctions_source_id""",
    )
    assert len(rows) == n
    for kind, full, name, sim, hit in rows:
        assert hit, (kind, full, name, sim)
        if kind == "exact":
            assert full == name and sim == 1.0
        else:
            assert full != name
    # the best trigram match for each planted name is its own sanctions row
    bad = _q(
        url,
        """SELECT count(*) FROM planted_matches p JOIN kyc_profiles k USING (entity_id)
           WHERE p.sanctions_source_id <> (
             SELECT source_id FROM sanctions_entities
             ORDER BY similarity(name, k.full_name) DESC, source_id LIMIT 1)""",
    )
    assert bad == [(0,)]
    snap = (
        _q(url, "SELECT * FROM planted_matches ORDER BY entity_id"),
        _q(url, "SELECT * FROM kyc_profiles ORDER BY entity_id"),
    )
    plant_matches(url, 42, 15)
    assert snap == (
        _q(url, "SELECT * FROM planted_matches ORDER BY entity_id"),
        _q(url, "SELECT * FROM kyc_profiles ORDER BY entity_id"),
    )


def test_regenerating_kyc_clears_stale_plants(url: str) -> None:
    plant_matches(url, 42, 15)
    generate_kyc(url, 42)
    assert _q(url, "SELECT count(*) FROM planted_matches") == [(0,)]


def test_media_generation_cache_and_nearest_neighbour(url: str, tmp_path: Path) -> None:
    generate_kyc(url, 42)
    cache = EmbeddingCache(tmp_path / "e.jsonl")
    emb = FakeEmbedder()
    res = generate_media(url, 42, emb, cache, rate=0.25)
    assert res.articles > 0 and emb.calls > 0
    rows = _q(
        url,
        "SELECT entity_id, headline, body, theme, is_decoy FROM adverse_media ORDER BY media_id",
    )
    assert len(rows) == res.articles
    assert any(r[4] for r in rows) and any(not r[4] for r in rows)
    assert all("{" not in r[1] + r[2] for r in rows)
    assert _q(url, "SELECT count(*) FROM adverse_media WHERE embedding IS NULL") == [
        (0,)
    ]

    # nearest neighbour of an article's own text is that article
    mid, head, body = _q(
        url,
        "SELECT media_id, headline, body FROM adverse_media ORDER BY media_id LIMIT 1",
    )[0]
    vec = FakeEmbedder().embed([head + "\n" + body])[0]
    lit = "[" + ",".join(repr(v) for v in vec) + "]"
    assert len(vec) == DIM
    got = _q(
        url,
        f"SELECT media_id FROM adverse_media ORDER BY embedding <=> '{lit}'::vector LIMIT 1",
    )
    assert got == [(mid,)]

    # warm cache -> zero API calls and identical rows
    emb2 = FakeEmbedder()
    res2 = generate_media(
        url, 42, emb2, EmbeddingCache(tmp_path / "e.jsonl"), rate=0.25
    )
    assert emb2.calls == 0 and res2.api_calls == 0
    assert (
        _q(
            url,
            "SELECT entity_id, headline, body, theme, is_decoy FROM adverse_media ORDER BY media_id",
        )
        == rows
    )


def test_media_dry_run_writes_nothing_and_calls_nothing(
    url: str, tmp_path: Path
) -> None:
    generate_kyc(url, 42)
    generate_media(
        url, 42, FakeEmbedder(), EmbeddingCache(tmp_path / "w.jsonl"), rate=0.25
    )
    before = _q(url, "SELECT * FROM adverse_media ORDER BY media_id")
    emb = FakeEmbedder()
    res = generate_media(
        url, 42, emb, EmbeddingCache(tmp_path / "other.jsonl"), rate=0.25, dry_run=True
    )
    assert emb.calls == 0 and res.dry_run and res.estimate.uncached > 0
    assert _q(url, "SELECT * FROM adverse_media ORDER BY media_id") == before


def test_migration_0002_up_down_up(url: str) -> None:
    tables = ("kyc_profiles", "sanctions_entities", "planted_matches", "adverse_media")
    q = "SELECT count(*) FROM information_schema.tables WHERE table_name = ANY(%s)"

    def present() -> int:
        with _conn(url) as c:
            return c.execute(q, (list(tables),)).fetchone()[0]  # type: ignore[index]

    assert present() == 4
    _alembic("downgrade", "0001")
    assert present() == 0
    assert _q(url, "SELECT count(*) FROM pg_tables WHERE tablename='transactions'") == [
        (1,)
    ]
    _alembic("upgrade", "head")
    assert present() == 4
    ext = {r[0] for r in _q(url, "SELECT extname FROM pg_extension")}
    assert {"vector", "pg_trgm"} <= ext


@pytest.mark.live
def test_live_openai_embedding() -> None:
    from app.synth.embeddings import OpenAIEmbedder

    key = get_settings().openai_api_key
    if not key:
        pytest.skip("no OPENAI_API_KEY")
    vec = OpenAIEmbedder(key).embed(["hello"])[0]
    assert len(vec) == DIM

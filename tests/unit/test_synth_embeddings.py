import math
from pathlib import Path

import pytest

from app.synth.embeddings import (
    DIM,
    EmbeddingCache,
    EmbeddingLimitError,
    FakeEmbedder,
    cache_key,
    embed_texts,
    estimate_cost,
)


def test_fake_embedder_is_deterministic_unit_vectors() -> None:
    f = FakeEmbedder()
    a, b = f.embed(["x", "y"]), f.embed(["x", "y"])
    assert a == b and a[0] != a[1] and len(a[0]) == DIM
    assert math.isclose(sum(v * v for v in a[0]), 1.0, rel_tol=1e-6)


def test_cache_key_depends_on_text_and_model() -> None:
    assert cache_key("a", "m") != cache_key("b", "m")
    assert cache_key("a", "m1") != cache_key("a", "m2")


def test_miss_then_hit_makes_no_second_call(tmp_path: Path) -> None:
    path = tmp_path / "e.jsonl"
    texts = [f"t{i}" for i in range(5)]
    emb = FakeEmbedder()
    r1 = embed_texts(texts, EmbeddingCache(path), emb, batch_size=2)
    assert r1.api_calls == 3 and emb.calls == 3
    # new cache object reads the file: no calls, same vectors
    emb2 = FakeEmbedder()
    r2 = embed_texts(texts, EmbeddingCache(path), emb2, batch_size=2)
    assert emb2.calls == 0 and r2.api_calls == 0
    assert r1.vectors == r2.vectors


def test_changed_text_only_reembeds_that_text(tmp_path: Path) -> None:
    cache = EmbeddingCache(tmp_path / "e.jsonl")
    embed_texts(["a", "b", "c"], cache, FakeEmbedder())
    emb = FakeEmbedder()
    r = embed_texts(["a", "B", "c"], cache, emb)
    assert r.embedded == 1 and emb.calls == 1


def test_duplicate_texts_embedded_once(tmp_path: Path) -> None:
    emb = FakeEmbedder()
    r = embed_texts(["a", "a", "a"], EmbeddingCache(tmp_path / "e.jsonl"), emb)
    assert r.embedded == 1 and list(r.vectors) == ["a"]


def test_limit_blocks_before_any_call(tmp_path: Path) -> None:
    emb = FakeEmbedder()
    with pytest.raises(EmbeddingLimitError):
        embed_texts(
            [f"t{i}" for i in range(10)],
            EmbeddingCache(tmp_path / "e.jsonl"),
            emb,
            batch_size=2,
            limit=4,
        )
    assert emb.calls == 0


def test_estimate_counts_uncached_only(tmp_path: Path) -> None:
    cache = EmbeddingCache(tmp_path / "e.jsonl")
    embed_texts(["a" * 400], cache, FakeEmbedder())
    est = estimate_cost(["a" * 400, "b" * 400, "b" * 400], cache)
    assert est.total == 2 and est.uncached == 1
    assert est.est_tokens == 100 and est.est_cost_usd == pytest.approx(100 * 0.02 / 1e6)

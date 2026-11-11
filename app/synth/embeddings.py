"""OpenAI embeddings with an on-disk cache (F03).

The cache is keyed by sha256(text) + model, so re-runs and DB resets make no
API calls and a changed article only re-embeds itself.
"""

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

MODEL = "text-embedding-3-small"
DIM = 1536
# USD per 1M input tokens, from OpenAI's published price (checked 2026-11-11;
# re-check before a real run). Token counts below are a ~4 chars/token estimate.
PRICE_PER_M_TOKENS = 0.02
DEFAULT_CACHE = Path("data/media_cache/embeddings.jsonl")


class EmbeddingLimitError(RuntimeError):
    pass


class Embedder(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]: ...


class OpenAIEmbedder:
    def __init__(self, api_key: str, model: str = MODEL) -> None:
        from openai import OpenAI

        self._client = OpenAI(api_key=api_key)
        self._model = model

    def embed(self, texts: list[str]) -> list[list[float]]:
        resp = self._client.embeddings.create(model=self._model, input=texts)
        data = sorted(resp.data, key=lambda d: d.index)
        return [list(d.embedding) for d in data]


class FakeEmbedder:
    """Deterministic unit vectors derived from a hash of the text (tests)."""

    def __init__(self) -> None:
        self.calls = 0

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        return [self._one(t) for t in texts]

    @staticmethod
    def _one(text: str) -> list[float]:
        out: list[float] = []
        counter = 0
        while len(out) < DIM:
            h = hashlib.sha256(f"{counter}|{text}".encode()).digest()
            out.extend((b - 127.5) / 127.5 for b in h)
            counter += 1
        vec = out[:DIM]
        norm = math.sqrt(sum(v * v for v in vec))
        return [v / norm for v in vec]


def cache_key(text: str, model: str = MODEL) -> str:
    return f"{hashlib.sha256(text.encode()).hexdigest()}:{model}"


class EmbeddingCache:
    def __init__(self, path: Path, model: str = MODEL) -> None:
        self.path, self.model = path, model
        self._data: dict[str, list[float]] = {}
        if path.is_file():
            with path.open(encoding="utf-8") as fh:
                for line in fh:
                    if line.strip():
                        rec = json.loads(line)
                        self._data[rec["key"]] = rec["embedding"]

    def get(self, text: str) -> list[float] | None:
        return self._data.get(cache_key(text, self.model))

    def put_many(self, texts: list[str], vectors: list[list[float]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            for t, v in zip(texts, vectors, strict=True):
                key = cache_key(t, self.model)
                self._data[key] = v
                fh.write(json.dumps({"key": key, "embedding": v}) + "\n")


@dataclass(frozen=True)
class Estimate:
    total: int
    uncached: int
    est_tokens: int
    est_cost_usd: float


def _uncached(texts: list[str], cache: EmbeddingCache) -> list[str]:
    seen: dict[str, None] = {}
    for t in texts:
        if cache.get(t) is None:
            seen.setdefault(t)
    return list(seen)


def estimate_cost(texts: list[str], cache: EmbeddingCache) -> Estimate:
    todo = _uncached(texts, cache)
    tokens = sum(len(t) // 4 for t in todo)
    return Estimate(
        total=len(set(texts)),
        uncached=len(todo),
        est_tokens=tokens,
        est_cost_usd=tokens * PRICE_PER_M_TOKENS / 1_000_000,
    )


@dataclass(frozen=True)
class EmbedResult:
    vectors: dict[str, list[float]]
    api_calls: int
    embedded: int


def embed_texts(
    texts: list[str],
    cache: EmbeddingCache,
    embedder: Embedder,
    *,
    batch_size: int = 100,
    limit: int | None = None,
) -> EmbedResult:
    """Embed texts, reading the cache first. `limit` caps API requests and is
    checked before any call, so a too-small limit never leaves a partial run."""
    todo = _uncached(texts, cache)
    batches = [todo[i : i + batch_size] for i in range(0, len(todo), batch_size)]
    if limit is not None and len(batches) > limit:
        raise EmbeddingLimitError(
            f"{len(batches)} API calls needed, --limit is {limit}"
        )
    for batch in batches:
        vectors = embedder.embed(batch)
        if len(vectors) != len(batch) or any(len(v) != DIM for v in vectors):
            raise RuntimeError("embedding response has unexpected shape")
        cache.put_many(batch, vectors)
    out: dict[str, list[float]] = {}
    for t in texts:
        vec = cache.get(t)
        assert vec is not None
        out[t] = vec
    return EmbedResult(vectors=out, api_calls=len(batches), embedded=len(todo))

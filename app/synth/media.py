"""Template-written adverse-media articles, embedded into pgvector (F03).

No LLM writes text here: articles come from app/synth/media_templates.json and
all numbers are formatted by Python.
"""

import json
import re
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import psycopg

from app.db.copy import conninfo, log
from app.synth.embeddings import (
    Embedder,
    EmbeddingCache,
    Estimate,
    embed_texts,
    estimate_cost,
)
from app.synth.rng import entity_rngs, entity_seed

TEMPLATES_PATH = Path(__file__).with_name("media_templates.json")
SUPPORTED_PLACEHOLDERS = frozenset(
    {
        "name",
        "short",
        "city",
        "country",
        "amount",
        "amount2",
        "year",
        "month",
        "outlet",
        "authority",
        "court",
        "industry",
        "counterparty",
        "person2",
    }
)
# Selection constants (printed on every run).
MEDIA_RATE = 0.02  # share of KYC entities that get an article
LAUNDERER_SHARE = 0.6  # of selected entities, aim for this share to be true launderers
_PLACEHOLDER = re.compile(r"\{(\w+)\}")
_MONTHS = [
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
]
_PUBLISHED_START = date(2021, 1, 1)
_PUBLISHED_DAYS = 730


@dataclass(frozen=True)
class MediaCandidate:
    entity_id: str
    entity_type: str
    is_launderer: bool


@dataclass(frozen=True)
class MediaPlan:
    entity_id: str
    entity_type: str
    is_decoy: bool


@dataclass(frozen=True)
class Article:
    entity_id: str
    theme: str
    is_decoy: bool
    headline: str
    body: str
    published_at: date
    combo: tuple[int, ...]  # template choices before names are filled in


def load_templates(path: Path = TEMPLATES_PATH) -> dict[str, Any]:
    with path.open(encoding="utf-8") as fh:
        data: dict[str, Any] = json.load(fh)
    return data


def select_media_entities(
    seed: int, candidates: list[MediaCandidate], rate: float = MEDIA_RATE
) -> list[MediaPlan]:
    """Pick ~rate of candidates: launderers (laundering themes) plus non-launderer decoys."""
    ranked = sorted(candidates, key=lambda c: entity_seed(seed, c.entity_id, "select"))
    launderers = [c for c in ranked if c.is_launderer]
    clean = [c for c in ranked if not c.is_launderer]
    total = round(rate * len(candidates))
    n_l = min(round(LAUNDERER_SHARE * total), len(launderers))
    n_d = min(total - n_l, len(clean))
    plans = [MediaPlan(c.entity_id, c.entity_type, False) for c in launderers[:n_l]]
    plans += [MediaPlan(c.entity_id, c.entity_type, True) for c in clean[:n_d]]
    return sorted(plans, key=lambda p: p.entity_id)


def _fill(text: str, ctx: dict[str, str]) -> str:
    return _PLACEHOLDER.sub(lambda m: ctx[m.group(1)], text)  # KeyError if unknown


def _money(rng: Any) -> str:
    return f"${rng.randint(40, 950) * 1000:,}"


def build_article(
    templates: dict[str, Any], plan: MediaPlan, full_name: str, seed: int
) -> Article:
    rng, fake = entity_rngs(seed, plan.entity_id, "media")
    audience = "person" if plan.entity_type == "Individual" else "org"
    category = "decoy" if plan.is_decoy else "laundering"
    themes = [
        (i, t)
        for i, t in enumerate(templates["themes"])
        if t["category"] == category and audience in t["applies_to"]
    ]
    t_idx, theme = rng.choice(themes)
    h = rng.randrange(len(theme["headlines"]))
    o = rng.randrange(len(theme["openings"]))
    d1, d2 = sorted(rng.sample(range(len(theme["details"])), 2))
    c = rng.randrange(len(theme["closings"]))

    published = _PUBLISHED_START + timedelta(days=rng.randrange(_PUBLISHED_DAYS))
    fillers = templates["fillers"]
    words = full_name.split()
    ctx = {
        "name": full_name,
        "short": words[-1] if plan.entity_type == "Individual" else words[0],
        "city": fake.city(),
        "country": fake.country(),
        "amount": _money(rng),
        "amount2": _money(rng),
        "year": str(published.year),
        "month": _MONTHS[published.month - 1],
        "outlet": rng.choice(fillers["outlets"]),
        "authority": rng.choice(fillers["authorities"]),
        "court": rng.choice(fillers["courts"]),
        "industry": rng.choice(fillers["industries"]),
        "counterparty": fake.company(),
        "person2": fake.name(),
    }
    body = " ".join(
        _fill(s, ctx)
        for s in (
            theme["openings"][o],
            theme["details"][d1],
            theme["details"][d2],
            theme["closings"][c],
        )
    )
    return Article(
        entity_id=plan.entity_id,
        theme=theme["id"],
        is_decoy=plan.is_decoy,
        headline=_fill(theme["headlines"][h], ctx),
        body=body,
        published_at=published,
        combo=(t_idx, h, o, d1, d2, c),
    )


@dataclass(frozen=True)
class MediaResult:
    articles: int
    launderer_articles: int
    decoy_articles: int
    api_calls: int
    dry_run: bool
    estimate: Estimate


_CANDIDATE_SQL = """
WITH l AS (
    SELECT from_account_id AS account_id FROM transactions WHERE is_laundering
    UNION
    SELECT to_account_id FROM transactions WHERE is_laundering
), le AS (
    SELECT DISTINCT a.entity_id FROM accounts a JOIN l USING (account_id)
)
SELECT k.entity_id, e.entity_type, k.full_name, le.entity_id IS NOT NULL
FROM kyc_profiles k
JOIN entities e USING (entity_id)
LEFT JOIN le USING (entity_id)
ORDER BY k.entity_id
"""


def _vec_literal(vec: list[float]) -> str:
    return "[" + ",".join(repr(float(v)) for v in vec) + "]"


def generate_media(
    database_url: str,
    seed: int,
    embedder: Embedder,
    cache: EmbeddingCache,
    *,
    rate: float = MEDIA_RATE,
    limit: int | None = None,
    dry_run: bool = False,
    batch_size: int = 100,
) -> MediaResult:
    """Truncate and reload adverse_media. With dry_run, only read and estimate."""
    templates = load_templates()
    with psycopg.connect(conninfo(database_url)) as conn, conn.cursor() as cur:
        rows = cur.execute(_CANDIDATE_SQL).fetchall()
        names = {r[0]: r[2] for r in rows}
        plans = select_media_entities(
            seed, [MediaCandidate(r[0], r[1], r[3]) for r in rows], rate
        )
        articles = [
            build_article(templates, p, names[p.entity_id], seed) for p in plans
        ]
        n_dec = sum(a.is_decoy for a in articles)
        log(
            f"media selection: rate={rate}, launderer_share={LAUNDERER_SHARE}, "
            f"{len(articles)} articles ({len(articles) - n_dec} launderer, {n_dec} decoy) "
            f"from {len(rows)} KYC entities"
        )
        texts = [f"{a.headline}\n{a.body}" for a in articles]
        est = estimate_cost(texts, cache)
        log(
            f"embeddings: {est.total} texts, {est.uncached} uncached, "
            f"~{est.est_tokens} tokens, est. ${est.est_cost_usd:.4f}"
        )
        if dry_run:
            return MediaResult(
                len(articles), len(articles) - n_dec, n_dec, 0, True, est
            )

        res = embed_texts(texts, cache, embedder, batch_size=batch_size, limit=limit)
        cur.execute("TRUNCATE adverse_media RESTART IDENTITY")
        cur.executemany(
            "INSERT INTO adverse_media (entity_id, headline, body, published_at, "
            "is_decoy, theme, embedding) VALUES (%s,%s,%s,%s,%s,%s,%s::vector)",
            [
                (
                    a.entity_id,
                    a.headline,
                    a.body,
                    a.published_at,
                    a.is_decoy,
                    a.theme,
                    _vec_literal(res.vectors[t]),
                )
                for a, t in zip(articles, texts, strict=True)
            ],
        )
        conn.commit()
    log(f"adverse_media: {len(articles)} rows, {res.api_calls} API calls")
    return MediaResult(
        len(articles), len(articles) - n_dec, n_dec, res.api_calls, False, est
    )

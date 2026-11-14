"""adverse_media: articles about an account holder, optionally ranked by topic (pgvector)."""

from typing import Any

import psycopg

from app.agent.evidence import Evidence, build_evidence
from app.agent.tools.common import cursor, entity_of, iso, run_tool
from app.agent.tools.schemas import AdverseMediaArgs
from app.synth.embeddings import Embedder

TOOL = "adverse_media"
SNIPPET_CHARS = 300


def adverse_media(
    conn: psycopg.Connection, embedder: Embedder | None = None, **params: Any
) -> Evidence:
    return run_tool(TOOL, AdverseMediaArgs, params, lambda a: _run(conn, embedder, a))


def _vector_literal(vec: list[float]) -> str:
    return "[" + ",".join(repr(float(v)) for v in vec) + "]"


def _run(
    conn: psycopg.Connection, embedder: Embedder | None, a: AdverseMediaArgs
) -> Evidence:
    args = a.model_dump(mode="json")
    if a.query is not None and embedder is None:
        return build_evidence(
            TOOL,
            args,
            "error",
            "Semantic search is unavailable; call again without `query`.",
        )
    with cursor(conn) as cur:
        who = entity_of(cur, a.account_id)
        if who is None:
            return build_evidence(
                TOOL,
                args,
                "empty",
                f"Account {a.account_id} does not exist.",
                {"account_exists": False},
            )
        eid = who["entity_id"]
        cur.execute(
            "SELECT count(*) AS n FROM adverse_media WHERE entity_id = %s", (eid,)
        )
        row = cur.fetchone()
        assert row is not None
        total = row["n"]
        if total == 0:
            return build_evidence(
                TOOL,
                args,
                "empty",
                f"No adverse media found for account {a.account_id}.",
                {"account_exists": True, "total_articles": 0, "articles": []},
            )

        cols = "media_id, headline, left(body, %(chars)s) AS snippet, published_at"  # deliberately excludes is_decoy and theme (ground truth)
        if a.query is None:
            cur.execute(
                f"SELECT {cols} FROM adverse_media WHERE entity_id = %(e)s "
                "ORDER BY published_at DESC, media_id LIMIT %(n)s",
                {"e": eid, "n": a.limit, "chars": SNIPPET_CHARS},
            )
            order = "newest first"
            rows = cur.fetchall()
        else:
            assert embedder is not None
            vec = _vector_literal(embedder.embed([a.query])[0])
            cur.execute(
                f"SELECT {cols}, 1 - (embedding <=> %(v)s::vector) AS similarity "
                "FROM adverse_media WHERE entity_id = %(e)s "
                "ORDER BY embedding <=> %(v)s::vector, media_id LIMIT %(n)s",
                {"e": eid, "n": a.limit, "v": vec, "chars": SNIPPET_CHARS},
            )
            order = "most similar to query first"
            rows = cur.fetchall()

    articles = []
    for r in rows:
        item = {
            "media_id": r["media_id"],
            "headline": r["headline"],
            "snippet": r["snippet"],
            "published_at": iso(r["published_at"]),
        }
        if "similarity" in r:
            item["similarity"] = round(float(r["similarity"]), 3)
        articles.append(item)
    summary = (
        f"{total} adverse media article(s) for account {a.account_id}; "
        f"showing {len(articles)} ({order}). Top: '{articles[0]['headline']}'."
    )
    data = {
        "account_exists": True,
        "total_articles": total,
        "order": order,
        "query": a.query,
        "articles": articles,
    }
    return build_evidence(TOOL, args, "ok", summary, data)

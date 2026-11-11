"""Generate template-written adverse media and embed it with OpenAI.

Always run with --dry-run first: it prints text count, uncached count and the
estimated cost, then exits without calling the API or writing to the database.

.venv/bin/python -m scripts.generate_media --dry-run
.venv/bin/python -m scripts.generate_media [--limit N]
"""

import argparse
from pathlib import Path

from sqlalchemy.engine import make_url

from app.config import get_settings
from app.synth.embeddings import (
    EmbeddingCache,
    EmbeddingLimitError,
    OpenAIEmbedder,
)
from app.synth.media import MEDIA_RATE, generate_media


def main() -> int:
    s = get_settings()
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--seed", type=int, default=s.seed)
    p.add_argument("--database-url", default=None)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--limit", type=int, default=None, help="max OpenAI API requests")
    p.add_argument("--rate", type=float, default=MEDIA_RATE)
    p.add_argument(
        "--cache", type=Path, default=s.data_dir / "media_cache/embeddings.jsonl"
    )
    a = p.parse_args()
    url = a.database_url or s.sync_database_url
    if not a.dry_run and not s.openai_api_key:
        # Never echo the key; only say whether it exists.
        print("OPENAI_API_KEY is not set in .env (needed unless the cache is warm)")
    print(
        f"Target database: {make_url(url).database}"
        + ("" if a.dry_run else " (adverse_media will be TRUNCATED)")
    )
    embedder = OpenAIEmbedder(s.openai_api_key or "unset")
    try:
        r = generate_media(
            url,
            a.seed,
            embedder,
            EmbeddingCache(a.cache),
            rate=a.rate,
            limit=a.limit,
            dry_run=a.dry_run,
        )
    except EmbeddingLimitError as exc:
        print(f"STOPPED before any API call: {exc}")
        return 1
    if r.dry_run:
        print("dry run: nothing written, no API calls made")
    else:
        print(f"{r.articles} articles written, {r.api_calls} API calls")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

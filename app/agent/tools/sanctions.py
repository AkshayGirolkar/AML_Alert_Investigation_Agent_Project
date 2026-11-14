"""sanctions_screen: fuzzy-match an account holder's KYC name against sanctions lists."""

import contextlib
from typing import Any

import psycopg

from app.agent.evidence import Evidence, build_evidence
from app.agent.tools.common import cursor, entity_of, run_tool
from app.agent.tools.schemas import SanctionsScreenArgs

TOOL = "sanctions_screen"
STRONG = 0.9
PROBABLE = 0.7


def _strength(sim: float) -> str:
    return "strong" if sim >= STRONG else "probable" if sim >= PROBABLE else "weak"


def sanctions_screen(conn: psycopg.Connection, **params: Any) -> Evidence:
    return run_tool(TOOL, SanctionsScreenArgs, params, lambda a: _run(conn, a))


def _run(conn: psycopg.Connection, a: SanctionsScreenArgs) -> Evidence:
    args = a.model_dump(mode="json")
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
        if not who["has_kyc"]:
            return build_evidence(
                TOOL,
                args,
                "empty",
                f"Account {a.account_id} has no KYC profile, so there is no name to screen.",
                {"account_exists": True, "has_kyc": False},
            )
        name: str = who["full_name"]
        country: str | None = who["country"]

        # `name % q` uses the GIN trigram index at the session limit; we set it for this
        # call and restore it, so a pooled connection is left as we found it.
        cur.execute("SELECT show_limit() AS lim")
        previous = cur.fetchone()
        assert previous is not None
        cur.execute("SELECT set_limit(%s::real)", (a.min_similarity,))
        try:
            cur.execute(
                """
                SELECT source_id, name, schema_type, countries, dataset,
                       similarity(name, %(q)s) AS sim
                FROM sanctions_entities
                WHERE name %% %(q)s AND similarity(name, %(q)s) >= %(t)s""",
                {"q": name, "t": a.min_similarity},
            )
            hits: dict[str, dict[str, Any]] = {
                r["source_id"]: {**r, "matched_on": "name", "matched_text": r["name"]}
                for r in cur.fetchall()
            }
        finally:
            with contextlib.suppress(psycopg.Error):
                cur.execute("SELECT set_limit(%s::real)", (previous["lim"],))
        # Aliases have no index, so this is a scan; the F10 cache will absorb repeats.
        cur.execute(
            """
            SELECT s.source_id, s.name, s.schema_type, s.countries, s.dataset,
                   al.alias AS matched_text, similarity(al.alias, %(q)s) AS sim
            FROM sanctions_entities s CROSS JOIN LATERAL unnest(s.aliases) AS al(alias)
            WHERE similarity(al.alias, %(q)s) >= %(t)s""",
            {"q": name, "t": a.min_similarity},
        )
        for r in cur.fetchall():
            best = hits.get(r["source_id"])
            if best is None or r["sim"] > best["sim"]:
                hits[r["source_id"]] = {**r, "matched_on": "alias"}

    ranked = sorted(hits.values(), key=lambda h: (-h["sim"], h["source_id"]))[: a.limit]
    matches = []
    for h in ranked:
        countries = [c.lower() for c in (h["countries"] or [])]
        matches.append(
            {
                "source_id": h["source_id"],
                "name": h["name"],
                "schema_type": h["schema_type"],
                "matched_on": h["matched_on"],
                "matched_text": h["matched_text"],
                "similarity": round(float(h["sim"]), 3),
                "strength": _strength(float(h["sim"])),
                "countries": h["countries"] or [],
                "datasets": h["dataset"] or [],
                "country_match": bool(country and country.lower() in countries),
            }
        )
    data = {
        "account_exists": True,
        "has_kyc": True,
        "screened_name": name,
        "screened_country": country,
        "min_similarity": a.min_similarity,
        "matches": matches,
    }
    if not matches:
        summary = (
            f"No sanctions match for '{name}' at similarity >= {a.min_similarity}."
        )
        return build_evidence(TOOL, args, "empty", summary, data)
    top = matches[0]
    summary = (
        f"{len(matches)} sanctions candidate(s) for '{name}'; best is '{top['name']}' "
        f"(similarity {top['similarity']}, {top['strength']}, matched on {top['matched_on']})."
    )
    return build_evidence(TOOL, args, "ok", summary, data)

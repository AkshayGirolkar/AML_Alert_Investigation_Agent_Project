"""LangChain wrappers for the F05 tools.

Every tool takes plain arguments, runs on its own short-lived read-only connection and
returns `Evidence.model_dump(mode="json")`. Argument validation happens inside the tool
(see `run_tool`), so bad input comes back as an error record instead of raising.
"""

from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Any

import psycopg
from langchain_core.tools import StructuredTool
from pydantic import BaseModel

from app.agent.tools.media import adverse_media
from app.agent.tools.network import counterparty_network
from app.agent.tools.peers import peer_comparison
from app.agent.tools.sanctions import sanctions_screen
from app.agent.tools.schemas import (
    AdverseMediaArgs,
    CounterpartyNetworkArgs,
    PeerComparisonArgs,
    QueryTransactionsArgs,
    SanctionsScreenArgs,
)
from app.agent.tools.transactions import query_transactions
from app.db.copy import conninfo
from app.synth.embeddings import Embedder

ConnFactory = Callable[[], AbstractContextManager[psycopg.Connection]]


def make_conn_factory(database_url: str) -> ConnFactory:
    """New read-only connection per call (the tools never write)."""

    def factory() -> AbstractContextManager[psycopg.Connection]:
        return psycopg.connect(
            conninfo(database_url), options="-c default_transaction_read_only=on"
        )

    return factory


def _tool(
    name: str,
    description: str,
    schema: type[BaseModel],
    conn_factory: ConnFactory,
    call: Callable[..., Any],
) -> StructuredTool:
    def run(**kwargs: Any) -> dict[str, Any]:
        with conn_factory() as conn:
            result = call(conn, **kwargs)
        return result.model_dump(mode="json")  # type: ignore[no-any-return]

    # A JSON schema (not the pydantic class) so LangChain does not validate first;
    # our own validation returns errors as data.
    return StructuredTool.from_function(
        func=run,
        name=name,
        description=description,
        args_schema=schema.model_json_schema(),
    )


def build_tools(
    conn_factory: ConnFactory, embedder: Embedder | None = None
) -> list[StructuredTool]:
    return [
        _tool(
            "query_transactions",
            "Summarise an account's transactions (counts, per-currency totals, formats, "
            "counterparties) with a sample of the latest ones. Optional time window.",
            QueryTransactionsArgs,
            conn_factory,
            query_transactions,
        ),
        _tool(
            "counterparty_network",
            "Map the accounts an account transacts with, up to 2 hops, with edge "
            "totals and any money cycles that return to the account.",
            CounterpartyNetworkArgs,
            conn_factory,
            counterparty_network,
        ),
        _tool(
            "sanctions_screen",
            "Fuzzy-match the account holder's name against sanctions lists.",
            SanctionsScreenArgs,
            conn_factory,
            sanctions_screen,
        ),
        _tool(
            "adverse_media",
            "Find adverse-media articles about the account holder; pass `query` to "
            "rank them by topic.",
            AdverseMediaArgs,
            conn_factory,
            lambda conn, **kw: adverse_media(conn, embedder, **kw),
        ),
        _tool(
            "peer_comparison",
            "Compare the holder's outgoing activity with peers of the same type and "
            "country, and with their expected monthly volume.",
            PeerComparisonArgs,
            conn_factory,
            peer_comparison,
        ),
    ]

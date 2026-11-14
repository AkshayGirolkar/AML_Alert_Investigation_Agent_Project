"""Unit tests for F05 (no DB, no network): evidence IDs, validation, registry."""

from contextlib import nullcontext
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from app.agent.evidence import build_evidence, make_evidence_id
from app.agent.tools.network import _cycles_through
from app.agent.tools.peers import _rank
from app.agent.tools.registry import build_tools
from app.agent.tools.schemas import (
    AdverseMediaArgs,
    CounterpartyNetworkArgs,
    PeerComparisonArgs,
    QueryTransactionsArgs,
    SanctionsScreenArgs,
)
from app.agent.tools.transactions import query_transactions

EXPECTED_TOOLS = {
    "query_transactions",
    "counterparty_network",
    "sanctions_screen",
    "adverse_media",
    "peer_comparison",
}


def test_evidence_id_is_deterministic_and_order_independent() -> None:
    a = make_evidence_id("t", {"x": 1, "y": [1, 2]})
    assert a == make_evidence_id("t", {"y": [1, 2], "x": 1})
    assert a.startswith("t:") and len(a) == len("t:") + 10


def test_evidence_id_differs_by_tool_and_args() -> None:
    base = make_evidence_id("t", {"x": 1})
    assert base != make_evidence_id("u", {"x": 1})
    assert base != make_evidence_id("t", {"x": 2})


def test_error_evidence_has_no_id_but_ok_and_empty_do() -> None:
    assert build_evidence("t", {}, "error", "bad").evidence_id is None
    assert build_evidence("t", {}, "ok", "fine").evidence_id is not None
    assert build_evidence("t", {}, "empty", "none").evidence_id is not None


@pytest.mark.parametrize(
    ("model", "bad"),
    [
        (QueryTransactionsArgs, {"account_id": 0}),
        (QueryTransactionsArgs, {"account_id": "abc"}),
        (QueryTransactionsArgs, {"account_id": 1, "direction": "sideways"}),
        (QueryTransactionsArgs, {"account_id": 1, "sample_size": 51}),
        (QueryTransactionsArgs, {"account_id": 1, "sample_size": 0}),
        (QueryTransactionsArgs, {"account_id": 1, "unknown": True}),
        (
            QueryTransactionsArgs,
            {"account_id": 1, "start": "2022-09-02", "end": "2022-09-01"},
        ),
        (
            QueryTransactionsArgs,
            {"account_id": 1, "start": "2022-09-01", "end": "2022-09-01"},
        ),
        (CounterpartyNetworkArgs, {"account_id": 1, "hops": 3}),
        (CounterpartyNetworkArgs, {"account_id": 1, "hops": 0}),
        (CounterpartyNetworkArgs, {"account_id": 1, "max_neighbors": 16}),
        (SanctionsScreenArgs, {"account_id": 1, "min_similarity": 0.1}),
        (SanctionsScreenArgs, {"account_id": 1, "limit": 11}),
        (AdverseMediaArgs, {"account_id": 1, "query": ""}),
        (AdverseMediaArgs, {"account_id": 1, "query": "x" * 501}),
        (PeerComparisonArgs, {"account_id": 1, "peer_sample": 5}),
    ],
)
def test_bad_arguments_are_rejected(model: type, bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        model(**bad)


def test_defaults_and_timezone_normalisation() -> None:
    a = QueryTransactionsArgs(account_id=1, start=datetime(2022, 9, 1, 12, tzinfo=UTC))
    assert a.start == datetime(2022, 9, 1, 12) and a.start.tzinfo is None
    assert a.direction == "both" and a.sample_size == 10
    assert CounterpartyNetworkArgs(account_id=1).hops == 2


def test_invalid_input_returns_error_evidence_without_touching_the_db() -> None:
    # conn=None would blow up if validation did not short-circuit first.
    ev = query_transactions(None, account_id=-5)  # type: ignore[arg-type]
    assert ev.status == "error"
    assert ev.evidence_id is None
    assert "account_id" in ev.summary


def test_registry_exposes_the_five_tools_with_schemas() -> None:
    tools = build_tools(lambda: nullcontext(None))  # type: ignore[arg-type,return-value]
    assert {t.name for t in tools} == EXPECTED_TOOLS
    for t in tools:
        assert t.description
        schema = t.args_schema
        assert isinstance(schema, dict)
        assert "account_id" in schema["properties"]
        assert "account_id" in schema["required"]


def test_registry_tool_returns_error_as_data_not_exception() -> None:
    tools = {t.name: t for t in build_tools(lambda: nullcontext(None))}  # type: ignore[arg-type,return-value]
    out = tools["counterparty_network"].invoke({"account_id": 1, "hops": 9})
    assert out["status"] == "error" and out["evidence_id"] is None


def test_rank_handles_empty_ties_and_extremes() -> None:
    assert _rank(5, []) is None
    assert _rank(5, [1, 2, 3]) == 1.0
    assert _rank(0, [1, 2, 3]) == 0.0
    assert _rank(2, [2, 2]) == 0.5  # all ties -> middle


def test_cycle_finder() -> None:
    edges = {(1, 2), (2, 1), (1, 3), (3, 4), (4, 1), (5, 6)}
    cycles = _cycles_through(1, edges, max_len=4, cap=5)
    assert cycles == [[1, 2], [1, 3, 4]]
    assert _cycles_through(1, {(1, 2), (2, 3)}, 4, 5) == []
    assert _cycles_through(1, {(1, 2), (2, 3), (3, 4), (4, 5), (5, 1)}, 4, 5) == []
    assert len(_cycles_through(1, {(1, 2), (2, 1), (1, 3), (3, 1)}, 4, 1)) == 1

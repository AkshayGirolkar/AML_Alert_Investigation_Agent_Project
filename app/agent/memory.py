"""Long-term per-customer memory (F09) over a LangGraph `BaseStore`.

One record per investigated alert under ("customer_history", <customer_key>). Records hold
the agent's verdict and the analyst's decision only, never ground-truth labels.
"""

import logging
from datetime import UTC, datetime
from typing import Any

from langgraph.store.base import BaseStore

from app.agent.state import InvestigationState

log = logging.getLogger("agent.memory")

NAMESPACE_ROOT = "customer_history"
FETCH_LIMIT = 50  # records read before sorting and trimming to the caller's limit


def customer_key(state: InvestigationState) -> str:
    """KYC entity if there is a profile, otherwise the account itself."""
    kyc = state.get("kyc")
    if kyc and kyc.get("entity_id"):
        return str(kyc["entity_id"])
    return f"account:{state['alert']['account_id']}"


def has_prior_approved_sar(history: list[dict[str, Any]]) -> bool:
    return any(r.get("decision") == "approve" for r in history)


def build_record(state: InvestigationState) -> dict[str, Any]:
    verdict = state.get("verdict") or {}
    return {
        "alert_id": state["alert_id"],
        "rule_id": state["alert"]["rule_id"],
        "outcome": state.get("outcome"),
        "disposition": verdict.get("disposition"),
        "typology": verdict.get("typology"),
        "risk_score": verdict.get("risk_score"),
        "confidence": verdict.get("confidence"),
        "decision": state.get("decision"),
        "analyst_id": state.get("analyst_id"),
        "analyst_notes": state.get("analyst_notes"),
        "recorded_at": datetime.now(UTC).isoformat(),
    }


class CustomerMemory:
    def __init__(self, store: BaseStore) -> None:
        self._store = store

    async def recall(
        self, state: InvestigationState, limit: int
    ) -> list[dict[str, Any]]:
        """Newest-first prior records for this customer. Never raises: a store outage
        must not stop an investigation, it just runs without history."""
        try:
            items = await self._store.asearch(
                (NAMESPACE_ROOT, customer_key(state)), limit=FETCH_LIMIT
            )
        except Exception:
            log.warning(
                "memory recall failed; continuing without history", exc_info=True
            )
            return []
        records = [
            dict(i.value)
            for i in items
            if i.value.get("alert_id")
            != state["alert_id"]  # not this alert's own record
        ]
        records.sort(
            key=lambda r: (r.get("recorded_at", ""), r.get("alert_id", 0)), reverse=True
        )
        return records[:limit]

    async def record(self, state: InvestigationState) -> None:
        """Idempotent: the key is the alert id, so a re-run overwrites. Errors propagate."""
        await self._store.aput(
            (NAMESPACE_ROOT, customer_key(state)),
            f"alert-{state['alert_id']}",
            build_record(state),
        )

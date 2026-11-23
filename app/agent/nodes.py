"""Graph nodes. `Nodes` holds the dependencies (DB, tools, LLMs, thresholds)."""

import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from langchain_core.tools import StructuredTool
from psycopg.rows import dict_row

from app.agent import prompts
from app.agent.evidence import Evidence
from app.agent.llm import LLM
from app.agent.routing import alert_context, check_grounding, escalation_reason
from app.agent.schemas import InvestigationPlan, SarDraft, Verdict
from app.agent.state import InvestigationState
from app.agent.tools.registry import ConnFactory
from app.config import Settings

# Only these columns reach the agent. `is_true_positive` and `typology` on the alerts
# row are ground truth and must never be selected.
ALERT_SQL = (
    "SELECT alert_id, rule_id, account_id, window_start, window_end, "
    "trigger_txn_ids, score FROM alerts WHERE alert_id = %s"
)
KYC_SQL = (
    "SELECT k.entity_id, k.full_name, k.occupation, k.country, "
    "k.expected_monthly_volume, k.risk_rating "
    "FROM accounts a JOIN kyc_profiles k ON k.entity_id = a.entity_id "
    "WHERE a.account_id = %s"
)


def jsonable(row: dict[str, Any]) -> dict[str, Any]:
    def conv(v: Any) -> Any:
        if isinstance(v, datetime | date):
            return v.isoformat()
        if isinstance(v, Decimal):
            return str(v)
        if isinstance(v, list):
            return [conv(i) for i in v]
        return v

    return {k: conv(v) for k, v in row.items()}


class Nodes:
    def __init__(
        self,
        conn_factory: ConnFactory,
        tools: list[StructuredTool],
        plan_llm: LLM,
        assess_llm: LLM,
        settings: Settings,
    ) -> None:
        self._conn_factory = conn_factory
        self._tools = {t.name: t for t in tools}
        self._plan_llm = plan_llm
        self._assess_llm = assess_llm
        self._settings = settings

    async def load_alert(self, state: InvestigationState) -> InvestigationState:
        async with (
            self._conn_factory() as conn,
            conn.cursor(row_factory=dict_row) as cur,
        ):
            row = await (await cur.execute(ALERT_SQL, (state["alert_id"],))).fetchone()
        if row is None:
            raise LookupError(f"alert {state['alert_id']} not found")
        alert = jsonable(row)
        alert.update(alert_context(alert, self._settings))
        return {
            "alert": alert,
            "evidence": [],
            "tool_calls": 0,
            "skipped_checks": [],
            "grounding_retries": 0,
            "grounding_errors": [],
        }

    async def load_kyc(self, state: InvestigationState) -> InvestigationState:
        async with (
            self._conn_factory() as conn,
            conn.cursor(row_factory=dict_row) as cur,
        ):
            row = await (
                await cur.execute(KYC_SQL, (state["alert"]["account_id"],))
            ).fetchone()
        return {"kyc": jsonable(row) if row else None}

    async def plan(self, state: InvestigationState) -> InvestigationState:
        tools = "\n".join(
            f"- {t.name}: {t.description} args={json.dumps(t.args)}"
            for t in self._tools.values()
        )
        plan = await self._plan_llm.structured(
            InvestigationPlan,
            [
                (
                    "system",
                    prompts.PLAN_SYSTEM.format(
                        max_calls=self._settings.max_tool_calls, tools=tools
                    ),
                ),
                ("user", prompts.plan_user(state["alert"], state.get("kyc"))),
            ],
        )
        return {"plan": [c.model_dump() for c in plan.checks]}

    async def tool_loop(self, state: InvestigationState) -> InvestigationState:
        """Run planned checks. The cap is enforced here, whatever the plan says."""
        account_id = state["alert"]["account_id"]
        evidence = list(state.get("evidence", []))
        seen = {e["evidence_id"] for e in evidence if e["evidence_id"]}
        calls = state.get("tool_calls", 0)
        skipped = list(state.get("skipped_checks", []))
        for check in state.get("plan", []):
            tool = self._tools.get(check["tool"])
            if tool is None:
                skipped.append(f"{check['tool']}: unknown tool")
                continue
            if calls >= self._settings.max_tool_calls:
                skipped.append(f"{check['tool']}: call cap reached")
                continue
            args = {"account_id": account_id, **check.get("args", {})}
            calls += 1
            record = Evidence.model_validate(await tool.ainvoke(args)).model_dump(
                mode="json"
            )
            if record["evidence_id"] is not None:
                if record["evidence_id"] in seen:
                    continue
                seen.add(record["evidence_id"])
            evidence.append(record)
        return {"evidence": evidence, "tool_calls": calls, "skipped_checks": skipped}

    async def assess(self, state: InvestigationState) -> InvestigationState:
        verdict = await self._assess_llm.structured(
            Verdict,
            [
                ("system", prompts.ASSESS_SYSTEM),
                (
                    "user",
                    prompts.assess_user(
                        state["alert"],
                        state.get("kyc"),
                        state.get("evidence", []),
                        state.get("grounding_errors", []),
                    ),
                ),
            ],
        )
        return {"verdict": verdict.model_dump()}

    async def validate_grounding(self, state: InvestigationState) -> InvestigationState:
        verdict = Verdict.model_validate(state["verdict"])
        problems = check_grounding(verdict, state.get("evidence", []))
        retries = state.get("grounding_retries", 0) + (1 if problems else 0)
        return {
            "grounded": not problems,
            "grounding_errors": problems,
            "grounding_retries": retries,
        }

    async def close_case(self, state: InvestigationState) -> InvestigationState:
        return {"outcome": "closed", "sar_draft": None}

    async def draft_sar(self, state: InvestigationState) -> InvestigationState:
        evidence = state.get("evidence", [])
        known = {e["evidence_id"] for e in evidence if e["evidence_id"]}
        verdict = state.get("verdict") or {}
        cfg = self._settings
        reason = escalation_reason(
            Verdict.model_validate(verdict) if verdict else None,
            state.get("grounded", False),
            cfg.close_risk_max,
            cfg.close_confidence_min,
            state["alert"]["rule_id"],
            cfg.no_auto_close_rule_set,
        )
        draft: SarDraft | None = None
        # If the model itself judged the alert benign (policy forced the escalation),
        # do not ask it to write a suspicious-activity narrative; state the reason.
        if state.get("grounded") and verdict.get("disposition") == "escalate":
            draft = await self._assess_llm.structured(
                SarDraft,
                [
                    ("system", prompts.SAR_SYSTEM),
                    (
                        "user",
                        prompts.sar_user(
                            state["alert"], state.get("kyc"), verdict, evidence
                        ),
                    ),
                ],
            )
            if not set(draft.evidence_ids) <= known or not draft.evidence_ids:
                draft = None  # the narrative cites something we cannot back up
        if draft is None:
            draft = _fallback_sar(state, reason)
        return {
            "outcome": "escalated",
            "escalation_reason": reason,
            "sar_draft": draft.model_dump(),
        }


def _fallback_sar(state: InvestigationState, reason: str | None = None) -> SarDraft:
    """Deterministic draft built only from tool summaries (no LLM, no invented facts)."""
    ok = [e for e in state.get("evidence", []) if e["evidence_id"]]
    lines = [f"- {e['summary']} [{e['evidence_id']}]" for e in ok]
    alert = state["alert"]
    head = (
        f"Alert {alert['alert_id']} (rule {alert['rule_id']}) on account "
        f"{alert['account_id']} escalated for analyst review."
    )
    if state.get("grounding_errors"):
        head += " The automated verdict could not be grounded in evidence."
    elif reason and (state.get("verdict") or {}).get("disposition") == "close":
        v = state["verdict"] or {}
        head += (
            f" Reason: {reason}. The agent assessed it as low risk "
            f"(risk {v['risk_score']:.2f}, confidence {v['confidence']:.2f}): "
            f"{v.get('rationale', '')}"
        )
    return SarDraft(
        narrative="\n".join([head, *lines]),
        evidence_ids=[e["evidence_id"] for e in ok],
    )

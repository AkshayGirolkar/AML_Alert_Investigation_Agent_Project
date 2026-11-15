"""LangGraph investigation graph (F06). Async; pass a checkpointer (F07) to make runs resumable.

load_alert -> load_kyc -> plan -> tool_loop -> assess -> validate_grounding
    -> grounded & benign & confident: close_case
    -> not grounded, first failure:   assess (retry once)
    -> otherwise:                     draft_sar
"""

from typing import Any, Literal

from langgraph.graph import END, START, StateGraph

from app.agent.llm import LLM
from app.agent.nodes import Nodes
from app.agent.routing import route_verdict
from app.agent.schemas import Verdict
from app.agent.state import InvestigationState
from app.agent.tools.registry import ConnFactory, build_tools
from app.config import Settings, get_settings
from app.synth.embeddings import Embedder

MAX_GROUNDING_RETRIES = 1


def build_graph(
    conn_factory: ConnFactory,
    plan_llm: LLM,
    assess_llm: LLM | None = None,
    embedder: Embedder | None = None,
    settings: Settings | None = None,
    checkpointer: Any = None,
) -> Any:
    settings = settings or get_settings()
    nodes = Nodes(
        conn_factory,
        build_tools(conn_factory, embedder),
        plan_llm,
        assess_llm or plan_llm,
        settings,
    )

    def after_validation(
        state: InvestigationState,
    ) -> Literal["assess", "close_case", "draft_sar"]:
        grounded = state.get("grounded", False)
        if not grounded and state.get("grounding_retries", 0) <= MAX_GROUNDING_RETRIES:
            return "assess"
        verdict = Verdict.model_validate(state["verdict"])
        return route_verdict(
            verdict,
            grounded,
            settings.close_risk_max,
            settings.close_confidence_min,
            state["alert"]["rule_id"],
            settings.no_auto_close_rule_set,
        )

    g = StateGraph(InvestigationState)
    g.add_node("load_alert", nodes.load_alert)
    g.add_node("load_kyc", nodes.load_kyc)
    g.add_node("plan", nodes.plan)
    g.add_node("tool_loop", nodes.tool_loop)
    g.add_node("assess", nodes.assess)
    g.add_node("validate_grounding", nodes.validate_grounding)
    g.add_node("close_case", nodes.close_case)
    g.add_node("draft_sar", nodes.draft_sar)

    g.add_edge(START, "load_alert")
    g.add_edge("load_alert", "load_kyc")
    g.add_edge("load_kyc", "plan")
    g.add_edge("plan", "tool_loop")
    g.add_edge("tool_loop", "assess")
    g.add_edge("assess", "validate_grounding")
    g.add_conditional_edges("validate_grounding", after_validation)
    g.add_edge("close_case", END)
    g.add_edge("draft_sar", END)
    return g.compile(checkpointer=checkpointer)

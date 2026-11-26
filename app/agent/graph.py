"""LangGraph investigation graph (F06). Async; pass a checkpointer (F07) to make runs resumable.

load_alert -> load_kyc -> recall_history (F09) -> plan -> tool_loop -> assess
    -> validate_grounding
    -> grounded & benign & confident (& no prior approved SAR): close_case -> write_memory
    -> not grounded, first failure:   assess (retry once)
    -> otherwise: draft_sar -> human_review (interrupt, F08) -> write_memory -> END

`human_review` needs a checkpointer: the graph stops there until it is resumed with
`Command(resume={"decision", "analyst_id", "notes"})`.
"""

from typing import Any, Literal

from langgraph.graph import END, START, StateGraph
from langgraph.store.base import BaseStore

from app.agent.llm import LLM
from app.agent.nodes import Nodes
from app.agent.routing import route_verdict
from app.agent.schemas import Verdict
from app.agent.state import InvestigationState
from app.agent.tools.registry import ConnFactory, build_tools
from app.config import Settings, get_settings
from app.services.sanctions_cache import SanctionsCache
from app.synth.embeddings import Embedder

MAX_GROUNDING_RETRIES = 1


def build_graph(
    conn_factory: ConnFactory,
    plan_llm: LLM,
    assess_llm: LLM | None = None,
    embedder: Embedder | None = None,
    settings: Settings | None = None,
    checkpointer: Any = None,
    store: BaseStore | None = None,
    sanctions_cache: SanctionsCache | None = None,
) -> Any:
    settings = settings or get_settings()
    nodes = Nodes(
        conn_factory,
        build_tools(conn_factory, embedder, sanctions_cache),
        plan_llm,
        assess_llm or plan_llm,
        settings,
        store,
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
            nodes.prior_sar(state),
        )

    g = StateGraph(InvestigationState)
    g.add_node("load_alert", nodes.load_alert)
    g.add_node("load_kyc", nodes.load_kyc)
    g.add_node("recall_history", nodes.recall_history)
    g.add_node("write_memory", nodes.write_memory)
    g.add_node("plan", nodes.plan)
    g.add_node("tool_loop", nodes.tool_loop)
    g.add_node("assess", nodes.assess)
    g.add_node("validate_grounding", nodes.validate_grounding)
    g.add_node("close_case", nodes.close_case)
    g.add_node("draft_sar", nodes.draft_sar)
    g.add_node("human_review", nodes.human_review)

    g.add_edge(START, "load_alert")
    g.add_edge("load_alert", "load_kyc")
    g.add_edge("load_kyc", "recall_history")
    g.add_edge("recall_history", "plan")
    g.add_edge("plan", "tool_loop")
    g.add_edge("tool_loop", "assess")
    g.add_edge("assess", "validate_grounding")
    g.add_conditional_edges("validate_grounding", after_validation)
    g.add_edge("close_case", "write_memory")
    g.add_edge("draft_sar", "human_review")
    g.add_edge("human_review", "write_memory")
    g.add_edge("write_memory", END)
    return g.compile(checkpointer=checkpointer)

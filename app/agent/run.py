"""CLI: investigate one alert.  python -m app.agent.run --alert-id 123 [--decision approve]

Escalated alerts stop for analyst review. The CLI uses an in-memory checkpointer, so the
decision must be given in the same run (--decision approve|reject, optional --notes).
"""

import argparse
import asyncio
import json
from typing import Any

from langgraph.checkpoint.memory import MemorySaver
from langgraph.store.base import BaseStore
from langgraph.store.memory import InMemoryStore
from langgraph.store.postgres import AsyncPostgresStore
from langgraph.types import Command

from app.agent.gateway import MemoryRecorder, build_gateway, case_scope
from app.agent.graph import build_graph
from app.agent.tools.registry import make_conn_factory
from app.config import get_settings
from app.db.copy import conninfo
from app.observability.logging import configure_logging
from app.observability.tracing import setup_tracing
from app.synth.embeddings import FakeEmbedder, OpenAIEmbedder


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Investigate one alert")
    parser.add_argument("--alert-id", type=int, required=True)
    parser.add_argument(
        "--json", action="store_true", help="print the full final state"
    )
    parser.add_argument("--decision", choices=["approve", "reject"])
    parser.add_argument("--analyst-id", default="cli")
    parser.add_argument("--notes")
    parser.add_argument(
        "--memory-pg",
        action="store_true",
        help="keep customer memory (F09) in Postgres so it persists between CLI runs",
    )
    args = parser.parse_args(argv)

    s = get_settings()
    configure_logging(s.log_format, s.log_level)
    setup_tracing()
    fake = s.llm_provider == "fake"  # F16: synthetic LLM, no OpenAI key needed
    if not fake and not s.openai_api_key:
        parser.error("OPENAI_API_KEY is not set (see .env.example)")

    recorder = MemoryRecorder()

    async def investigate(store: BaseStore) -> dict[str, Any]:
        graph = build_graph(
            make_conn_factory(s.sync_database_url),
            plan_llm=build_gateway(
                s, "plan", s.llm_plan_model, s.llm_fallback_plan_model, recorder
            ),
            assess_llm=build_gateway(
                s, "assess", s.llm_assess_model, s.llm_fallback_assess_model, recorder
            ),
            embedder=FakeEmbedder() if fake else OpenAIEmbedder(s.openai_api_key),
            settings=s,
            checkpointer=MemorySaver(),
            store=store,
        )
        config = {"configurable": {"thread_id": f"cli-{args.alert_id}"}}
        with case_scope(args.alert_id):  # CLI has no case row; alert id stands in
            state: dict[str, Any] = await graph.ainvoke(
                {"alert_id": args.alert_id}, config
            )
            if state.get("__interrupt__") and args.decision:
                resume = {
                    "decision": args.decision,
                    "analyst_id": args.analyst_id,
                    "notes": args.notes,
                }
                state = await graph.ainvoke(Command(resume=resume), config)
        return state

    async def main_async() -> dict[str, Any]:
        if not args.memory_pg:  # in-process memory: forgotten when the CLI exits
            return await investigate(InMemoryStore())
        async with AsyncPostgresStore.from_conn_string(
            conninfo(s.sync_database_url)
        ) as store:
            await store.setup()
            return await investigate(store)

    state = asyncio.run(main_async())
    paused = bool(state.get("__interrupt__"))

    if args.json:
        print(json.dumps(state, indent=2, default=str))
        return 0
    v = state["verdict"]
    label = "AWAITING REVIEW" if paused else state["outcome"].upper()
    print(f"Alert {args.alert_id}: {label}")
    if v is None:
        print("  no verdict: LLM cost budget exceeded, a human must decide")
    else:
        print(
            f"  disposition={v['disposition']} typology={v['typology']} "
            f"risk={v['risk_score']:.2f} confidence={v['confidence']:.2f} "
            f"grounded={state['grounded']}"
        )
        print(f"  rationale: {v['rationale']}")
    print(
        f"  LLM: {len(recorder.records)} call(s), "
        f"{sum(r.input_tokens for r in recorder.records)} in / "
        f"{sum(r.output_tokens for r in recorder.records)} out tokens, "
        f"${recorder.total_cost:.4f}"
    )
    print(f"  tool calls: {state['tool_calls']}  skipped: {state['skipped_checks']}")
    for e in state["evidence"]:
        print(f"  [{e['evidence_id']}] {e['status']}: {e['summary']}")
    if state.get("history"):
        print(f"  prior cases on this customer: {len(state['history'])}")
    if state.get("sar_draft"):
        print("\nSAR draft:\n" + state["sar_draft"]["narrative"])
    if paused:
        print("\nPaused for analyst review. Re-run with --decision approve|reject.")
    elif state.get("decision"):
        print(f"\nAnalyst {state['analyst_id']}: {state['decision']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

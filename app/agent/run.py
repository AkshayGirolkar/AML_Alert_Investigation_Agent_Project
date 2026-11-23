"""CLI: investigate one alert.  python -m app.agent.run --alert-id 123 [--decision approve]

Escalated alerts stop for analyst review. The CLI uses an in-memory checkpointer, so the
decision must be given in the same run (--decision approve|reject, optional --notes).
"""

import argparse
import asyncio
import json
from typing import Any

from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from app.agent.graph import build_graph
from app.agent.llm import OpenAILLM
from app.agent.tools.registry import make_conn_factory
from app.config import get_settings
from app.synth.embeddings import OpenAIEmbedder


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Investigate one alert")
    parser.add_argument("--alert-id", type=int, required=True)
    parser.add_argument(
        "--json", action="store_true", help="print the full final state"
    )
    parser.add_argument("--decision", choices=["approve", "reject"])
    parser.add_argument("--analyst-id", default="cli")
    parser.add_argument("--notes")
    args = parser.parse_args(argv)

    s = get_settings()
    if not s.openai_api_key:
        parser.error("OPENAI_API_KEY is not set (see .env.example)")
    graph = build_graph(
        make_conn_factory(s.sync_database_url),
        plan_llm=OpenAILLM(s.llm_plan_model, s.openai_api_key),
        assess_llm=OpenAILLM(s.llm_assess_model, s.openai_api_key),
        embedder=OpenAIEmbedder(s.openai_api_key),
        settings=s,
        checkpointer=MemorySaver(),
    )

    async def investigate() -> dict[str, Any]:
        config = {"configurable": {"thread_id": f"cli-{args.alert_id}"}}
        state: dict[str, Any] = await graph.ainvoke({"alert_id": args.alert_id}, config)
        if state.get("__interrupt__") and args.decision:
            resume = {
                "decision": args.decision,
                "analyst_id": args.analyst_id,
                "notes": args.notes,
            }
            state = await graph.ainvoke(Command(resume=resume), config)
        return state

    state = asyncio.run(investigate())
    paused = bool(state.get("__interrupt__"))

    if args.json:
        print(json.dumps(state, indent=2, default=str))
        return 0
    v = state["verdict"]
    label = "AWAITING REVIEW" if paused else state["outcome"].upper()
    print(f"Alert {args.alert_id}: {label}")
    print(
        f"  disposition={v['disposition']} typology={v['typology']} "
        f"risk={v['risk_score']:.2f} confidence={v['confidence']:.2f} "
        f"grounded={state['grounded']}"
    )
    print(f"  rationale: {v['rationale']}")
    print(f"  tool calls: {state['tool_calls']}  skipped: {state['skipped_checks']}")
    for e in state["evidence"]:
        print(f"  [{e['evidence_id']}] {e['status']}: {e['summary']}")
    if state.get("sar_draft"):
        print("\nSAR draft:\n" + state["sar_draft"]["narrative"])
    if paused:
        print("\nPaused for analyst review. Re-run with --decision approve|reject.")
    elif state.get("decision"):
        print(f"\nAnalyst {state['analyst_id']}: {state['decision']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

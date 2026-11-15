# F06 – LangGraph Investigation Graph v1

## Goal
Investigate a single alert and produce a grounded verdict: close it, or escalate with a draft SAR. Runs synchronously from a CLI.

## In scope
- State schema, nodes (`load_alert`, `load_kyc`, `plan`, `tool_loop`, `assess`, `validate_grounding`, `close_case`, `draft_sar`) and routing.
- `Verdict`, `InvestigationPlan`, `SarDraft` Pydantic models.
- A small LLM protocol (OpenAI via LangChain, plus a scripted fake for tests).
- CLI: `python -m app.agent.run --alert-id N`.
- Settings: `llm_plan_model`, `llm_assess_model`, `max_tool_calls`, `close_risk_max`, `close_confidence_min`.
- Dependencies: `langgraph`, `langchain`, `langchain-openai` added to `requirements.txt`.

## Out of scope
SQS/worker, checkpointing (`PostgresSaver`), `interrupt()` / analyst approval, long-term memory (`recall_history`), Redis, provider fallback and retries (F12), LangSmith/PII masking, new tables or migrations.

## Interfaces
- `Verdict {disposition: close|escalate, typology: str|None, risk_score 0-1, confidence 0-1, evidence_ids: list[str]}`.
- `build_graph(conn_factory, plan_llm, assess_llm=None, embedder=None, settings=None)` returns a compiled graph; `invoke({"alert_id": N})` returns the final state.
- Final state carries `outcome`: `closed` or `escalated` (SAR draft attached), plus `evidence`, `verdict`, `grounded`.

## Decisions
- Ground truth (`is_true_positive`, `typology` on `alerts`) is never loaded: alert columns are selected explicitly.
- The 8-call cap is enforced in code, not in the prompt. Duplicate calls collapse because evidence IDs are deterministic. Error results are kept in state but are not citable.
- The LLM never does arithmetic; all numbers come from tool evidence.
- Fail safe: a verdict that is still ungrounded after one retry is forced to `escalate`. Auto-close needs `disposition == close`, grounded, `risk_score <= close_risk_max` and `confidence >= close_confidence_min`.
- Rules the agent may never auto-close: `NO_AUTO_CLOSE_RULES` (default `fan_in,high_risk_counterparty`). In a 113-alert batch the agent closed 9 of 35 real laundering alerts it had not been tuned on, 7 of them in these two rules, where no signal separating true from false alerts was found. A model verdict of "close" on these rules is still escalated, with `escalation_reason` set and a deterministic draft (the model is not asked to write a SAR for an alert it judged benign). Revisit when better evidence exists for these rules.
- The model's `confidence` is not informative (every closure reported 0.90) and its `risk_score` moves in 0.1 steps, so the thresholds are a weak gate on their own.
- No `cases/evidence/decisions` tables yet (F07/F08).

## Acceptance criteria
- Tool loop stops at 8 calls.
- Verdicts citing unknown evidence IDs are rejected and retried once; still ungrounded means escalate.
- Unit tests for routing, loop cap and grounding with a fake LLM; one integration test on a real alert row.
- One alert can be investigated end-to-end from the CLI.

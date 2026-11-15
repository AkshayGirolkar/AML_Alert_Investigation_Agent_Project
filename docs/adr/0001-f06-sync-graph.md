# ADR 0001: F06 graph runs synchronously and reuses the F05 tools as-is

**Status:** accepted (F06)

**Context:** The F05 tools are sync psycopg functions returning `Evidence`. Checkpointing, queueing and async execution arrive in F07.

**Decision:** The F06 graph is compiled without a checkpointer and run with `invoke`. Evidence is stored in graph state as plain dicts, so F07 can checkpoint it without changes. Planning is plan-then-execute (one structured plan, then a code-bounded loop) rather than free-form tool calling, so the call cap and the grounding check are deterministic.

**Consequences:** No persistence of cases or evidence until F07/F08. Moving to async only needs `ainvoke` and `asyncio.to_thread` around the tools.

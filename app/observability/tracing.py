"""LangSmith tracing with PII masking (F13).

Tracing stays off unless LANGSMITH_TRACING=true and a key is set. When on, the shared
LangSmith client is created with hooks that mask every run's inputs, outputs and
metadata before it leaves the process. Failures here never break the app.
"""

import logging
import os
from typing import Any

from dotenv import load_dotenv

from app.observability.pii import mask

log = logging.getLogger("tracing")


def _hide(data: Any) -> Any:
    return mask(data) if isinstance(data, dict) else data


def tracing_enabled(env: dict[str, str] | None = None) -> bool:
    env = dict(os.environ) if env is None else env
    flag = env.get("LANGSMITH_TRACING", env.get("LANGCHAIN_TRACING_V2", ""))
    key = env.get("LANGSMITH_API_KEY", env.get("LANGCHAIN_API_KEY", ""))
    return flag.lower() == "true" and bool(key)


def setup_tracing(env: dict[str, str] | None = None) -> bool:
    """Create the masking client and make LangChain/LangGraph use it. Returns active?"""
    if env is None:
        load_dotenv()  # LangSmith reads os.environ, not our Settings object
    if not tracing_enabled(env):
        return False
    try:
        import langsmith.run_trees as rt

        client = rt.get_cached_client(
            hide_inputs=_hide, hide_outputs=_hide, hide_metadata=_hide
        )
        if client._hide_inputs is not _hide:  # type: ignore[attr-defined]
            # A client existed before we got here: refuse to trace unmasked.
            log.error("LangSmith client already created without masking; disabling")
            os.environ["LANGSMITH_TRACING"] = "false"
            os.environ["LANGCHAIN_TRACING_V2"] = "false"
            return False
        return True
    except Exception:
        log.exception("could not set up LangSmith tracing; continuing without it")
        return False


def case_trace_config(case_id: int, alert_id: int, thread_id: str) -> dict[str, Any]:
    """Run config that tags a case's trace so it can be found in LangSmith."""
    return {
        "run_name": f"investigate-alert-{alert_id}",
        "tags": ["aml-investigation"],
        "metadata": {"case_id": case_id, "alert_id": alert_id, "thread_id": thread_id},
    }

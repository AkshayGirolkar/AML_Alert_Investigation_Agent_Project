"""Evidence records returned by every agent tool (F05).

The F06 graph appends each record to state; `validate_grounding` then checks that every
evidence ID cited in a verdict exists there. IDs are deterministic (tool + arguments),
so repeating a call yields the same ID and cannot create duplicate evidence.
"""

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

Status = Literal["ok", "empty", "error"]


def make_evidence_id(tool: str, args: dict[str, Any]) -> str:
    canonical = json.dumps(args, sort_keys=True, separators=(",", ":"), default=str)
    return f"{tool}:{hashlib.sha256(canonical.encode()).hexdigest()[:10]}"


class Evidence(BaseModel):
    model_config = ConfigDict(frozen=True)

    # None for status="error": a failed call is not evidence and cannot be cited.
    evidence_id: str | None
    tool: str
    status: Status
    summary: str  # one sentence, built in Python (never by an LLM)
    data: dict[str, Any]
    args: dict[str, Any]


def build_evidence(
    tool: str,
    args: dict[str, Any],
    status: Status,
    summary: str,
    data: dict[str, Any] | None = None,
) -> Evidence:
    return Evidence(
        evidence_id=None if status == "error" else make_evidence_id(tool, args),
        tool=tool,
        status=status,
        summary=summary,
        data=data or {},
        args=args,
    )

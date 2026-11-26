"""Structured outputs of the F06 graph (what the LLM must return)."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# The IBM patterns vocabulary (same names the scorer uses, app/eval/typology.py).
Typology = Literal[
    "FAN-OUT",
    "FAN-IN",
    "CYCLE",
    "SCATTER-GATHER",
    "GATHER-SCATTER",
    "STACK",
    "BIPARTITE",
    "RANDOM",
]

ToolName = Literal[
    "query_transactions",
    "counterparty_network",
    "sanctions_screen",
    "adverse_media",
    "peer_comparison",
]


class PlannedCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool: str = Field(description="One of the available tool names.")
    args: dict[str, Any] = Field(
        default_factory=dict,
        description="Tool arguments. account_id defaults to the alert's account.",
    )
    reason: str = Field(default="", description="Why this check helps the decision.")


class InvestigationPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    checks: list[PlannedCheck] = Field(default_factory=list)


class Verdict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    disposition: Literal["close", "escalate"]
    typology: Typology | None = Field(
        default=None,
        description="Closest laundering pattern from the allowed names, or null.",
    )
    risk_score: float = Field(ge=0.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)
    evidence_ids: list[str] = Field(
        default_factory=list,
        description="IDs of the evidence records that support the verdict.",
    )
    rationale: str = Field(default="", description="Short reasoning for the analyst.")

    @classmethod
    def from_state(cls, data: dict[str, Any] | None) -> "Verdict":
        """Read a verdict stored in graph state or a checkpoint.

        Verdicts written before F19 carry free-text typologies; map those to the closed
        set (or null) instead of failing a case that is resuming after a deploy.
        """
        from app.eval.typology import normalize_typology

        fixed = dict(data or {})
        fixed["typology"] = normalize_typology(fixed.get("typology"))
        return cls.model_validate(fixed)


class SarDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    narrative: str
    evidence_ids: list[str] = Field(default_factory=list)

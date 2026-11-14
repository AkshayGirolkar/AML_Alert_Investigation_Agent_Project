"""Validated arguments for the F05 tools. They also supply the JSON schema the LLM sees."""

from datetime import UTC, datetime
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class AccountArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_id: int = Field(ge=1, description="Numeric account id (e.g. the alert's).")


class WindowArgs(AccountArgs):
    start: datetime | None = Field(
        default=None,
        description="Inclusive window start (ISO 8601). Omit for no bound.",
    )
    end: datetime | None = Field(
        default=None, description="Exclusive window end (ISO 8601). Omit for no bound."
    )

    @field_validator("start", "end")
    @classmethod
    def _naive_utc(cls, v: datetime | None) -> datetime | None:
        # The database stores naive timestamps (no timezone in the source data).
        if v is not None and v.tzinfo is not None:
            return v.astimezone(UTC).replace(tzinfo=None)
        return v

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.start and self.end and self.start >= self.end:
            raise ValueError("start must be before end")
        return self


class QueryTransactionsArgs(WindowArgs):
    direction: Literal["in", "out", "both"] = "both"
    sample_size: int = Field(
        default=10, ge=1, le=50, description="Latest N rows to list."
    )


class CounterpartyNetworkArgs(WindowArgs):
    hops: int = Field(default=2, ge=1, le=2)
    max_neighbors: int = Field(
        default=5, ge=1, le=15, description="Top counterparties kept per account."
    )


class SanctionsScreenArgs(AccountArgs):
    min_similarity: float = Field(default=0.5, ge=0.3, le=1.0)
    limit: int = Field(default=5, ge=1, le=10)


class AdverseMediaArgs(AccountArgs):
    query: str | None = Field(
        default=None,
        min_length=1,
        max_length=500,
        description="Optional topic to rank articles by (semantic search).",
    )
    limit: int = Field(default=5, ge=1, le=10)


class PeerComparisonArgs(WindowArgs):
    peer_sample: int = Field(default=500, ge=10, le=2000)

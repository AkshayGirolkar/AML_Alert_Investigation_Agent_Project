"""Request models for the API (F08)."""

from typing import Literal

from pydantic import BaseModel, Field


class DecisionRequest(BaseModel):
    decision: Literal["approve", "reject"]
    # With AUTH_MODE=jwt the analyst is the token's user and this may be omitted (or must
    # match it). With auth off the caller states who they are.
    analyst_id: str | None = Field(default=None, min_length=1, max_length=200)
    notes: str | None = Field(default=None, max_length=5000)

"""Request models for the API (F08)."""

from typing import Literal

from pydantic import BaseModel, Field


class DecisionRequest(BaseModel):
    decision: Literal["approve", "reject"]
    # No auth until F11, so the caller states who they are.
    analyst_id: str = Field(min_length=1, max_length=200)
    notes: str | None = Field(default=None, max_length=5000)

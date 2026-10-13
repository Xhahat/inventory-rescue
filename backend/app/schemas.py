from __future__ import annotations

from pydantic import BaseModel, Field


class MessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=8000)
    source: str = Field("paste", max_length=60)


class DecisionIn(BaseModel):
    reason: str | None = Field(None, max_length=500)


class PlanIn(BaseModel):
    budget: float | None = Field(None, ge=0, le=10_000_000)


class ScenarioIn(BaseModel):
    question: str = Field(min_length=3, max_length=1000)
    budget: float | None = Field(None, ge=0, le=10_000_000)


class ReforecastIn(BaseModel):
    sku_ids: list[str] | None = None
    force: bool = False

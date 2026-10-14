from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class MessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=8000)
    source: str = Field("paste", max_length=60)


class DecisionIn(BaseModel):
    reason: str | None = Field(None, max_length=500)


class PlanIn(BaseModel):
    budget: float | None = Field(None, ge=0, le=10_000_000)
    strategy: Literal["commit_now", "staggered", "reserve", "lean"] = "commit_now"
    # Share of the budget the "reserve" option keeps back on day 0; defaults to the configured reserve_share.
    reserve_share: float | None = Field(None, ge=0.05, le=0.9)


class ScenarioIn(BaseModel):
    question: str = Field(min_length=3, max_length=1000)
    budget: float | None = Field(None, ge=0, le=10_000_000)


class ReforecastIn(BaseModel):
    sku_ids: list[str] | None = None
    force: bool = False

class OverrideItem(BaseModel):
    sku_id: str = Field(min_length=1, max_length=24)
    order_qty: float | None = Field(None, ge=0, le=1_000_000)
    skip: bool = False
    discount_pct: float | None = Field(None, ge=0, le=0.9)
    expedite: bool | None = None


class OverridesIn(BaseModel):
    items: list[OverrideItem] = Field(min_length=1, max_length=200)


class PriorityIn(BaseModel):
    priority: Literal["hero", "standard", "auto"]

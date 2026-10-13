from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import JSON, Date, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Supplier(Base):
    __tablename__ = "suppliers"
    supplier_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    stated_lead_time_days: Mapped[int] = mapped_column(Integer)
    expedite_lead_time_days: Mapped[int] = mapped_column(Integer)
    expedite_premium_pct: Mapped[float] = mapped_column(Float)
    terms: Mapped[str] = mapped_column(Text, default="")


class Product(Base):
    __tablename__ = "products"
    sku_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    name: Mapped[str] = mapped_column(String(160))
    category: Mapped[str] = mapped_column(String(60))
    supplier_id: Mapped[str] = mapped_column(ForeignKey("suppliers.supplier_id"))
    unit_cost: Mapped[float] = mapped_column(Float)
    unit_price: Mapped[float] = mapped_column(Float)
    case_pack: Mapped[int] = mapped_column(Integer)
    moq: Mapped[int] = mapped_column(Integer)
    shelf_life_days: Mapped[int] = mapped_column(Integer)
    image_key: Mapped[str | None] = mapped_column(String(200), nullable=True)


class Sale(Base):
    __tablename__ = "sales"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sale_date: Mapped[date] = mapped_column(Date, index=True)
    sku_id: Mapped[str] = mapped_column(ForeignKey("products.sku_id"), index=True)
    channel: Mapped[str] = mapped_column(String(40))
    segment: Mapped[str] = mapped_column(String(40))
    units: Mapped[int] = mapped_column(Integer)
    __table_args__ = (Index("ix_sales_sku_date", "sku_id", "sale_date"),)


class InventoryLot(Base):
    __tablename__ = "inventory_lots"
    lot_id: Mapped[str] = mapped_column(String(24), primary_key=True)
    sku_id: Mapped[str] = mapped_column(ForeignKey("products.sku_id"), index=True)
    qty: Mapped[int] = mapped_column(Integer)
    received_date: Mapped[date] = mapped_column(Date)
    expiry_date: Mapped[date] = mapped_column(Date)


class PurchaseOrder(Base):
    """Open (not yet received) purchase orders."""

    __tablename__ = "purchase_orders"
    po_id: Mapped[str] = mapped_column(String(24), primary_key=True)
    sku_id: Mapped[str] = mapped_column(ForeignKey("products.sku_id"), index=True)
    supplier_id: Mapped[str] = mapped_column(ForeignKey("suppliers.supplier_id"))
    qty: Mapped[int] = mapped_column(Integer)
    order_date: Mapped[date] = mapped_column(Date)
    expected_date: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(16), default="open")


class Receipt(Base):
    __tablename__ = "receipts"
    receipt_id: Mapped[str] = mapped_column(String(24), primary_key=True)
    supplier_id: Mapped[str] = mapped_column(ForeignKey("suppliers.supplier_id"), index=True)
    sku_id: Mapped[str] = mapped_column(ForeignKey("products.sku_id"))
    po_date: Mapped[date] = mapped_column(Date)
    expected_date: Mapped[date] = mapped_column(Date)
    received_date: Mapped[date] = mapped_column(Date)
    ordered_qty: Mapped[int] = mapped_column(Integer)
    received_qty: Mapped[int] = mapped_column(Integer)


class ContextEvent(Base):
    """Promotions, holidays, weather, competitor prices, social signals."""

    __tablename__ = "context_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(20), index=True)  # promotion|holiday|weather|competitor|social
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    sku_id: Mapped[str | None] = mapped_column(String(16), nullable=True)
    category: Mapped[str | None] = mapped_column(String(60), nullable=True)
    value: Mapped[float] = mapped_column(Float, default=0.0)
    description: Mapped[str] = mapped_column(Text, default="")


class EvaluationDemand(Base):
    """Simulated 'actual' demand path used only for the value scorecard."""

    __tablename__ = "evaluation_demand"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sku_id: Mapped[str] = mapped_column(String(16), index=True)
    demand_date: Mapped[date] = mapped_column(Date)
    units: Mapped[float] = mapped_column(Float)


class Message(Base):
    __tablename__ = "messages"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(60), default="paste")
    text: Mapped[str] = mapped_column(Text)
    storage_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    extraction_method: Mapped[str] = mapped_column(String(20), default="llm")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SignalEvent(Base):
    __tablename__ = "signal_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    message_id: Mapped[int] = mapped_column(ForeignKey("messages.id"), index=True)
    event_type: Mapped[str] = mapped_column(String(30))
    sku_ids: Mapped[list] = mapped_column(JSON, default=list)
    magnitude: Mapped[float] = mapped_column(Float, default=0.0)
    start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    quoted_evidence: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending|confirmed|rejected|invalid
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Forecast(Base):
    __tablename__ = "forecasts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sku_id: Mapped[str] = mapped_column(String(16), index=True)
    as_of: Mapped[date] = mapped_column(Date)
    input_hash: Mapped[str] = mapped_column(String(64), index=True)
    method: Mapped[str] = mapped_column(String(20))  # llm|fallback
    fallback_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    p10: Mapped[list] = mapped_column(JSON)
    p50: Mapped[list] = mapped_column(JSON)
    p90: Mapped[list] = mapped_column(JSON)
    drivers: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class LLMCall(Base):
    __tablename__ = "llm_calls"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task: Mapped[str] = mapped_column(String(30), index=True)
    input_hash: Mapped[str] = mapped_column(String(64), index=True)
    model: Mapped[str] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(16))  # ok|ok_repaired|invalid|error|unavailable
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    output: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Plan(Base):
    __tablename__ = "plans"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    as_of: Mapped[date] = mapped_column(Date)
    budget: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(16), default="draft")  # draft|approved
    actions: Mapped[list] = mapped_column(JSON)
    baseline_actions: Mapped[list] = mapped_column(JSON)
    summary: Mapped[dict] = mapped_column(JSON)
    explanation: Mapped[dict] = mapped_column(JSON)
    scenario: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    exports: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Alert(Base):
    __tablename__ = "alerts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    entity_id: Mapped[str] = mapped_column(String(24))
    alert_type: Mapped[str] = mapped_column(String(30))
    severity: Mapped[str] = mapped_column(String(10))
    message: Mapped[str] = mapped_column(Text)
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    active: Mapped[bool] = mapped_column(default=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (UniqueConstraint("entity_id", "alert_type", name="uq_alert_entity_type"),)


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(16), default="queued")  # queued|running|done|failed
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AppState(Base):
    __tablename__ = "app_state"
    key: Mapped[str] = mapped_column(String(60), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON)

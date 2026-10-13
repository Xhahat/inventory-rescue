"""Loads DB state into pure domain objects; builds planning and evaluation worlds; refreshes forecasts."""

from __future__ import annotations

import json
import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..adapters.storage import get_storage
from ..config import get_settings
from ..llm.client import get_llm, persist_records
from ..models import (AppState, ContextEvent, EvaluationDemand, Forecast, InventoryLot, Product, PurchaseOrder, Receipt, Sale,
                      SignalEvent, Supplier)
from . import forecasting as fc
from .simulator import Inbound, Lot, ProductEconomics
from .supply import ReceiptRecord, SupplierMetrics, supplier_metrics
from .world import ForecastBand, OpenPO, SkuContext, SkuWorld, World

PATH_WEIGHTS = (0.25, 0.5, 0.25)


@dataclass
class Snapshot:
    as_of: date
    horizon: int
    products: dict[str, Product]
    suppliers: dict[str, Supplier]
    metrics: dict[str, SupplierMetrics]
    history: dict[str, np.ndarray]
    lots: dict[str, list[InventoryLot]]
    open_pos: dict[str, list[PurchaseOrder]]
    context: list[ContextEvent]
    confirmed: list[SignalEvent]
    forecasts: dict[str, Forecast]
    eval_demand: dict[str, list[float]]
    truth_po_delays: dict[str, int]


def get_as_of(db: Session) -> date:
    last = db.scalar(select(func.max(Sale.sale_date)))
    return (last + timedelta(days=1)) if last else date.today()


def load_snapshot(db: Session, as_of: date | None = None) -> Snapshot:
    s = get_settings()
    as_of = as_of or get_as_of(db)
    H = s.horizon_days
    products = {p.sku_id: p for p in db.scalars(select(Product).order_by(Product.sku_id))}
    suppliers = {x.supplier_id: x for x in db.scalars(select(Supplier).order_by(Supplier.supplier_id))}

    receipts: dict[str, list[ReceiptRecord]] = defaultdict(list)
    for r in db.scalars(select(Receipt).where(Receipt.received_date < as_of)):
        receipts[r.supplier_id].append(ReceiptRecord(r.po_date, r.expected_date, r.received_date, r.ordered_qty,
                                                     r.received_qty))
    metrics = {sid: supplier_metrics(sid, sup.stated_lead_time_days, receipts.get(sid, []))
               for sid, sup in suppliers.items()}

    rows = db.execute(
        select(Sale.sku_id, Sale.sale_date, func.sum(Sale.units))
        .where(Sale.sale_date < as_of)
        .group_by(Sale.sku_id, Sale.sale_date)
    ).all()
    first = min((r[1] for r in rows), default=as_of - timedelta(days=1))
    n = max(1, (as_of - first).days)
    history = {sku: np.zeros(n) for sku in products}
    for sku, d, units in rows:
        if sku in history:
            history[sku][(d - first).days] = float(units or 0)

    lots: dict[str, list[InventoryLot]] = defaultdict(list)
    for l in db.scalars(select(InventoryLot).where(InventoryLot.qty > 0).order_by(InventoryLot.expiry_date)):
        lots[l.sku_id].append(l)
    open_pos: dict[str, list[PurchaseOrder]] = defaultdict(list)
    for p in db.scalars(select(PurchaseOrder).where(PurchaseOrder.status == "open").order_by(PurchaseOrder.po_id)):
        open_pos[p.sku_id].append(p)

    context = list(db.scalars(select(ContextEvent).where(
        ContextEvent.end_date >= as_of - timedelta(days=56), ContextEvent.start_date < as_of + timedelta(days=H))))
    confirmed = list(db.scalars(select(SignalEvent).where(SignalEvent.status == "confirmed").order_by(SignalEvent.id)))

    forecasts: dict[str, Forecast] = {}
    for f in db.scalars(select(Forecast).where(Forecast.as_of == as_of).order_by(Forecast.id)):
        forecasts[f.sku_id] = f  # latest wins

    ev: dict[str, dict[date, float]] = defaultdict(dict)
    for e in db.scalars(select(EvaluationDemand).where(
            EvaluationDemand.demand_date >= as_of, EvaluationDemand.demand_date < as_of + timedelta(days=H))):
        ev[e.sku_id][e.demand_date] = e.units
    eval_demand = {}
    for sku, m in ev.items():
        if len(m) == H:
            eval_demand[sku] = [m[as_of + timedelta(days=i)] for i in range(H)]

    truth = db.get(AppState, "evaluation_truth")
    truth_delays = dict((truth.value or {}).get("po_extra_delay_days", {})) if truth else {}

    return Snapshot(as_of, H, products, suppliers, metrics, history, lots, open_pos, context, confirmed, forecasts,
                    eval_demand, truth_delays)


# ---------------------------------------------------------------- events

def _active(e: SignalEvent, as_of: date) -> bool:
    return e.end_date is None or e.end_date >= as_of


def demand_events(snap: Snapshot, sku_id: str) -> list[fc.EventLike]:
    p = snap.products[sku_id]
    out = []
    for e in snap.confirmed:
        if e.event_type in ("demand_spike", "demand_drop") and sku_id in (e.sku_ids or []) and e.magnitude > 0:
            start = e.start_date or snap.as_of
            end = e.end_date or (start + timedelta(days=fc.DEFAULT_EVENT_DAYS - 1))
            out.append(fc.EventLike(e.id, e.event_type, start, end, float(e.magnitude),
                                    f"Confirmed signal EV-{e.id}: {e.event_type.replace('_', ' ')}"))
    for c in snap.context:
        if c.kind == "promotion" and (c.sku_id == sku_id or (c.sku_id is None and c.category == p.category)) \
                and c.end_date >= snap.as_of:
            out.append(fc.EventLike(f"PROMO-{c.id}", "promotion", c.start_date, c.end_date, 1.0 + c.value,
                                    f"Planned promotion PROMO-{c.id}: {c.description}"))
    return out


def delay_events(snap: Snapshot, sku_id: str) -> list[SignalEvent]:
    return [e for e in snap.confirmed if e.event_type == "supplier_delay" and sku_id in (e.sku_ids or [])
            and _active(e, snap.as_of) and e.magnitude > 0]


def context_rows(snap: Snapshot, sku_id: str) -> list[dict]:
    p = snap.products[sku_id]
    rows = []
    for c in snap.context:
        if c.sku_id not in (None, sku_id):
            continue
        if c.category not in (None, p.category):
            continue
        if c.kind == "promotion" and c.sku_id is None and c.category is None:
            continue
        rows.append({"kind": c.kind, "start": c.start_date.isoformat(), "end": c.end_date.isoformat(),
                     "value": c.value, "note": c.description[:120]})
    rows.sort(key=lambda r: (r["start"], r["kind"]))
    return rows


# ---------------------------------------------------------------- forecasts

def forecast_job(snap: Snapshot, sku_id: str, model: str) -> dict:
    p = snap.products[sku_id]
    events = demand_events(snap, sku_id)
    sku_info = {"sku_id": sku_id, "name": p.name, "category": p.category, "shelf_life_days": p.shelf_life_days,
                "unit_price_inr": p.unit_price}
    summary = fc.build_summary(sku_info, snap.history[sku_id], snap.as_of, snap.horizon, events, context_rows(snap, sku_id))
    return {"sku_id": sku_id, "summary": summary, "history": snap.history[sku_id], "as_of": snap.as_of,
            "horizon": snap.horizon, "events": events, "hash": fc.forecast_hash(summary, model)}


def current_model_name() -> str:
    llm = get_llm()
    return llm.model if llm is not None else "none"


def freshness(snap: Snapshot) -> dict[str, dict]:
    model = current_model_name()
    out = {}
    for sku in snap.products:
        f = snap.forecasts.get(sku)
        h = forecast_job(snap, sku, model)["hash"]
        out[sku] = {
            "forecast_id": f.id if f else None,
            "method": f.method if f else None,
            "created_at": f.created_at.isoformat() if f else None,
            "fresh": bool(f and f.input_hash == h),
        }
    return out


def refresh_forecasts(db: Session, sku_ids: list[str] | None = None, force: bool = False) -> dict:
    snap = load_snapshot(db)
    llm = get_llm()
    model = llm.model if llm is not None else "none"
    targets = sku_ids or list(snap.products)
    jobs = []
    reused = []
    for sku in targets:
        if sku not in snap.products:
            continue
        job = forecast_job(snap, sku, model)
        existing = snap.forecasts.get(sku)
        reusable = (lambda f: f is not None and (f.method == "llm" or llm is None))
        if not force and reusable(existing) and existing.input_hash == job["hash"]:
            reused.append(sku)
            continue
        cached = None if force else db.scalars(
            select(Forecast).where(Forecast.input_hash == job["hash"], Forecast.sku_id == sku)
            .order_by(Forecast.id.desc()).limit(1)).first()
        if reusable(cached):
            db.add(Forecast(sku_id=sku, as_of=snap.as_of, input_hash=cached.input_hash, method=cached.method,
                            fallback_reason=cached.fallback_reason, p10=cached.p10, p50=cached.p50, p90=cached.p90,
                            drivers=cached.drivers))
            reused.append(sku)
            continue
        file_hit = None if force else _cache_get(job["hash"])
        if file_hit is not None:
            db.add(Forecast(sku_id=sku, as_of=snap.as_of, input_hash=job["hash"], method="llm", fallback_reason=None,
                            p10=file_hit["p10"], p50=file_hit["p50"], p90=file_hit["p90"],
                            drivers=file_hit.get("drivers", [])))
            reused.append(sku)
            continue
        jobs.append(job)
    outputs = fc.forecast_many(llm, jobs, get_settings().llm_max_concurrency)
    methods = {}
    for o in outputs:
        db.add(Forecast(sku_id=o.sku_id, as_of=snap.as_of, input_hash=o.input_hash, method=o.method,
                        fallback_reason=o.fallback_reason, p10=o.p10, p50=o.p50, p90=o.p90, drivers=o.drivers))
        persist_records(db, [r for r in o.records if r.status != "unavailable"])
        methods[o.sku_id] = o.method
        if o.method == "llm":
            _cache_put(o.input_hash, {"p10": o.p10, "p50": o.p50, "p90": o.p90, "drivers": o.drivers})
    db.commit()
    return {"as_of": snap.as_of.isoformat(), "computed": methods, "reused": reused}


def _cache_get(h: str) -> dict | None:
    try:
        st = get_storage()
        key = f"cache/forecast/{h}.json"
        return json.loads(st.get(key)) if st.exists(key) else None
    except (OSError, ValueError):
        return None


def _cache_put(h: str, value: dict) -> None:
    try:
        get_storage().put(f"cache/forecast/{h}.json", json.dumps(value).encode("utf-8"))
    except OSError:
        pass


def band_for(snap: Snapshot, sku_id: str) -> ForecastBand:
    f = snap.forecasts.get(sku_id)
    if f is not None:
        return ForecastBand(list(f.p10), list(f.p50), list(f.p90), f.method, f.id, list(f.drivers or []))
    p10, p50, p90, drivers = fc.fallback_forecast(snap.history[sku_id], snap.as_of, snap.horizon,
                                                  demand_events(snap, sku_id))
    return ForecastBand(p10, p50, p90, "fallback", None, drivers)


# ---------------------------------------------------------------- contexts & worlds

def build_contexts(snap: Snapshot) -> list[SkuContext]:
    ctxs = []
    for sku, p in snap.products.items():
        sup = snap.suppliers[p.supplier_id]
        ctxs.append(SkuContext(
            sku_id=sku, name=p.name, category=p.category, supplier_id=p.supplier_id,
            econ=ProductEconomics(p.unit_cost, p.unit_price, p.shelf_life_days),
            moq=p.moq, case_pack=p.case_pack, stated_lead_time=sup.stated_lead_time_days,
            expedite_lead_time=sup.expedite_lead_time_days, expedite_premium_pct=sup.expedite_premium_pct,
            lots=[Lot(l.lot_id, float(l.qty), (l.expiry_date - snap.as_of).days) for l in snap.lots.get(sku, [])],
            open_pos=[OpenPO(o.po_id, float(o.qty), (o.expected_date - snap.as_of).days, o.supplier_id)
                      for o in snap.open_pos.get(sku, [])],
            history=snap.history[sku], metrics=snap.metrics[p.supplier_id], forecast=band_for(snap, sku),
        ))
    return ctxs


def _planning_supply(snap: Snapshot, ctx: SkuContext) -> tuple[int, int, list[Inbound], list[str], list[int]]:
    """Open POs slip by the supplier's P80 delay; a confirmed delay signal replaces that with the stated delay
    (whichever is larger). New orders keep the effective lead time (stated + P80)."""
    delays = delay_events(snap, ctx.sku_id)
    extra = int(max((math.ceil(e.magnitude) for e in delays), default=0))
    p80 = int(math.ceil(ctx.metrics.p80_delay_days - 1e-9))
    notes = [f"Confirmed signal EV-{e.id}: supplier delay +{e.magnitude:g} days on open POs" for e in delays]
    inbound = [Inbound(o.po_id, o.qty, o.expected_day + max(p80, extra)) for o in ctx.open_pos]
    return ctx.metrics.effective_lead_time_days, ctx.expedite_lead_time, inbound, notes, [e.id for e in delays]


def band_paths(p10, p50, p90) -> list[tuple[float, np.ndarray]]:
    """Turn daily quantile bands into three demand paths whose *cumulative* demand matches the cumulative
    p10/p90 under independent daily errors (spread grows with sqrt(t) instead of compounding every day)."""
    p10, p50, p90 = (np.asarray(x, dtype=float) for x in (p10, p50, p90))
    c50 = np.cumsum(p50)
    up = np.sqrt(np.cumsum((p90 - p50) ** 2))
    dn = np.sqrt(np.cumsum((p50 - p10) ** 2))
    hi = np.diff(np.concatenate([[0.0], c50 + up]))
    lo = np.maximum(0.0, np.diff(np.concatenate([[0.0], np.maximum(0.0, c50 - dn)])))
    return [(PATH_WEIGHTS[0], lo), (PATH_WEIGHTS[1], p50), (PATH_WEIGHTS[2], hi)]


def planning_world(snap: Snapshot, ctxs: list[SkuContext]) -> World:
    skus = {}
    for ctx in ctxs:
        f = ctx.forecast
        paths = band_paths(f.p10, f.p50, f.p90)
        std, exp, inbound, notes, ids = _planning_supply(snap, ctx)
        dem = demand_events(snap, ctx.sku_id)
        for e in dem:
            if isinstance(e.id, int):
                notes.append(f"Forecast includes confirmed signal EV-{e.id} (demand x{e.multiplier:g})")
                ids.append(e.id)
        skus[ctx.sku_id] = SkuWorld(paths, std, exp, inbound, notes, ids)
    return World("planning", skus, {"demand": "forecast low/mid/high paths (cumulative p10/p50/p90) weighted 25/50/25",
                                    "lead_time": "open POs: expected + max(P80 delay, confirmed delay); new POs: stated + P80"})


def evaluation_world(snap: Snapshot, ctxs: list[SkuContext]) -> World:
    skus = {}
    sources = set()
    for ctx in ctxs:
        if ctx.sku_id in snap.eval_demand:
            path = np.array(snap.eval_demand[ctx.sku_id])
            sources.add("simulated actual demand (seed)")
        else:
            path = np.array(ctx.forecast.p50)
            sources.add("forecast p50")
        typical = int(round(ctx.metrics.mean_delay_days))
        inbound = [Inbound(o.po_id, o.qty, o.expected_day + int(snap.truth_po_delays.get(o.po_id, typical)))
                   for o in ctx.open_pos]
        skus[ctx.sku_id] = SkuWorld([(1.0, path)], ctx.stated_lead_time + typical, ctx.expedite_lead_time, inbound)
    return World("evaluation", skus, {"demand": " + ".join(sorted(sources)),
                                      "lead_time": "open POs: seeded actual arrival (else expected + mean delay); "
                                                   "new POs: stated + mean historical delay; expedite on time",
                                      "note": "Same world for plan and baseline; independent of confirmed signals"})

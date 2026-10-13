"""Loads DB state into pure domain objects; builds planning and evaluation worlds; refreshes forecasts."""

from __future__ import annotations

import json
import math
from collections import defaultdict
from dataclasses import dataclass, replace
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
from .world import ForecastBand, OpenPO, SkuContext, SkuWorld, SupplyCase, World

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
    delay_samples: dict[str, list[int]] = None  # supplier -> historical days late (received - expected, >= 0)
    priority_overrides: dict[str, str] = None  # sku -> hero | standard (manual override)


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

    # whole history window: past events are divided out of history before fitting the statistical model
    context = list(db.scalars(select(ContextEvent).where(ContextEvent.start_date < as_of + timedelta(days=H))
                              .order_by(ContextEvent.start_date, ContextEvent.id)))
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
    delay_samples = {sid: sorted(max(0, (r.received_date - r.expected_date).days) for r in rs)
                     for sid, rs in receipts.items()}
    pri = db.get(AppState, "sku_priority")
    overrides = {k: v for k, v in dict((pri.value or {}) if pri else {}).items() if v in ("hero", "standard")}

    return Snapshot(as_of, H, products, suppliers, metrics, history, lots, open_pos, context, confirmed, forecasts,
                    eval_demand, truth_delays, delay_samples, overrides)


# ---------------------------------------------------------------- events

def _active(e: SignalEvent, as_of: date) -> bool:
    return e.end_date is None or e.end_date >= as_of


def demand_events(snap: Snapshot, sku_id: str, include_signals: bool = True) -> list[fc.EventLike]:
    """Confirmed demand signals (fixed multipliers) + SKU/category-scoped context events (priors = 1 + value).
    Context events without a SKU or category scope are informational only."""
    p = snap.products[sku_id]
    out = []
    if include_signals:
        for e in snap.confirmed:
            if e.event_type in ("demand_spike", "demand_drop") and sku_id in (e.sku_ids or []) and e.magnitude > 0:
                start = e.start_date or snap.as_of
                end = e.end_date or (start + timedelta(days=fc.DEFAULT_EVENT_DAYS - 1))
                out.append(fc.EventLike(e.id, e.event_type, start, end, float(e.magnitude),
                                        f"Confirmed signal EV-{e.id}: {e.event_type.replace('_', ' ')}"))
    for c in snap.context:
        scoped = c.sku_id == sku_id or (c.sku_id is None and c.category is not None and c.category == p.category)
        if scoped and c.value and 1.0 + c.value > 0:
            out.append(fc.EventLike(f"CTX-{c.id}", c.kind, c.start_date, c.end_date, 1.0 + float(c.value),
                                    f"{c.kind.capitalize()} CTX-{c.id}: {c.description}", "context"))
    return out


def delay_events(snap: Snapshot, sku_id: str) -> list[SignalEvent]:
    return [e for e in snap.confirmed if e.event_type == "supplier_delay" and sku_id in (e.sku_ids or [])
            and _active(e, snap.as_of) and e.magnitude > 0]


def context_rows(snap: Snapshot, sku_id: str) -> list[dict]:
    p = snap.products[sku_id]
    rows = []
    for c in snap.context:
        if c.end_date < snap.as_of - timedelta(days=56):
            continue
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
    base = fc.statistical_forecast(snap.history[sku_id], snap.as_of, snap.horizon, events)[4]
    summary = fc.build_summary(sku_info, snap.history[sku_id], snap.as_of, snap.horizon, events,
                               context_rows(snap, sku_id), base.base_p50)
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
        reusable = (lambda f: f is not None and (f.method == "hybrid" or llm is None))
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
            db.add(Forecast(sku_id=sku, as_of=snap.as_of, input_hash=job["hash"], method="hybrid", fallback_reason=None,
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
        if o.method == "hybrid":
            _cache_put(o.input_hash, {"p10": o.p10, "p50": o.p50, "p90": o.p90, "drivers": o.drivers})
    db.commit()
    return {"as_of": snap.as_of.isoformat(), "computed": methods, "reused": reused}


def _cache_get(h: str) -> dict | None:
    try:
        st = get_storage()
        key = f"cache/forecast/{h}.json"
        return json.loads(st.get(key)) if st.exists(key) else None
    except Exception:  # cache only: local OSError, bad JSON or Cloud Storage API errors
        return None


def _cache_put(h: str, value: dict) -> None:
    try:
        get_storage().put(f"cache/forecast/{h}.json", json.dumps(value).encode("utf-8"))
    except Exception:  # cache only
        pass


def band_for(snap: Snapshot, sku_id: str) -> ForecastBand:
    f = snap.forecasts.get(sku_id)
    if f is not None:
        return ForecastBand(list(f.p10), list(f.p50), list(f.p90), f.method, f.id, list(f.drivers or []))
    p10, p50, p90, drivers, _ = fc.statistical_forecast(snap.history[sku_id], snap.as_of, snap.horizon,
                                                        demand_events(snap, sku_id))
    return ForecastBand(p10, p50, p90, "statistical", None, drivers)


# ---------------------------------------------------------------- contexts & worlds

def sku_priorities(snap: Snapshot) -> dict[str, dict]:
    """Hero SKUs = top `hero_share` by trailing-28-day revenue, unless manually overridden."""
    s = get_settings()
    rev = {sku: float(np.sum(snap.history[sku][-28:])) * p.unit_price for sku, p in snap.products.items()}
    n_hero = max(1, int(round(len(rev) * s.hero_share))) if rev else 0
    ranked = sorted(rev, key=lambda k: (-rev[k], k))
    auto = {sku: ("hero" if i < n_hero else "standard") for i, sku in enumerate(ranked)}
    overrides = snap.priority_overrides or {}
    return {sku: {"priority": overrides.get(sku, auto[sku]), "auto": auto[sku], "overridden": sku in overrides,
                  "revenue_28d": round(rev[sku], 2)} for sku in snap.products}


def build_contexts(snap: Snapshot) -> list[SkuContext]:
    s = get_settings()
    pri = sku_priorities(snap)
    margins: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for sku, p in snap.products.items():
        margins[p.category].append((sku, p.unit_price - p.unit_cost))
    ctxs = []
    for sku, p in snap.products.items():
        sup = snap.suppliers[p.supplier_id]
        others = [m for k, m in margins[p.category] if k != sku]
        priority = pri[sku]["priority"]
        pct = s.goodwill_hero_pct if priority == "hero" else s.goodwill_standard_pct
        ctxs.append(SkuContext(
            sku_id=sku, name=p.name, category=p.category, supplier_id=p.supplier_id,
            econ=ProductEconomics(p.unit_cost, p.unit_price, p.shelf_life_days),
            moq=p.moq, case_pack=p.case_pack, stated_lead_time=sup.stated_lead_time_days,
            expedite_lead_time=sup.expedite_lead_time_days, expedite_premium_pct=sup.expedite_premium_pct,
            lots=[Lot(l.lot_id, float(l.qty), (l.expiry_date - snap.as_of).days) for l in snap.lots.get(sku, [])],
            open_pos=[OpenPO(o.po_id, float(o.qty), (o.expected_date - snap.as_of).days, o.supplier_id)
                      for o in snap.open_pos.get(sku, [])],
            history=snap.history[sku], metrics=snap.metrics[p.supplier_id], forecast=band_for(snap, sku),
            priority=priority, goodwill_per_unit=pct * p.unit_price,
            substitute_margin=float(np.mean(others)) if others else 0.0,
        ))
    return ctxs


# Lead-time uncertainty: three supplier-delay cases from the empirical delay distribution.
SUPPLY_QUANTILES = ((0.3, 15, "early (P15 delay)"), (0.4, 50, "typical (P50 delay)"), (0.3, 85, "late (P85 delay)"))


def _planning_supply(snap: Snapshot, ctx: SkuContext) -> tuple[int, int, list[Inbound], list[str], list[int]]:
    """Nominal supply (shown to users): open POs slip by the supplier's P80 delay; a confirmed delay signal
    replaces that with the stated delay (whichever is larger). New orders keep the effective lead time (stated + P80)."""
    delays = delay_events(snap, ctx.sku_id)
    extra = int(max((math.ceil(e.magnitude) for e in delays), default=0))
    p80 = int(math.ceil(ctx.metrics.p80_delay_days - 1e-9))
    notes = [f"Confirmed signal EV-{e.id}: supplier delay +{e.magnitude:g} days on open POs" for e in delays]
    inbound = [Inbound(o.po_id, o.qty, o.expected_day + max(p80, extra)) for o in ctx.open_pos]
    return ctx.metrics.effective_lead_time_days, ctx.expedite_lead_time, inbound, notes, [e.id for e in delays]


def supply_cases(snap: Snapshot, ctx: SkuContext) -> list[SupplyCase]:
    """Early / typical / late supplier cases (weights 30/40/30). In each case every PO from the supplier slips by
    the same delay (delays are correlated within a supplier); a confirmed delay signal is a floor for open POs.
    Expedited freight is assumed on time."""
    samples = (snap.delay_samples or {}).get(ctx.supplier_id) or [0]
    extra = int(max((math.ceil(e.magnitude) for e in delay_events(snap, ctx.sku_id)), default=0))
    out = []
    for w, q, label in SUPPLY_QUANTILES:
        d = int(math.ceil(float(np.percentile(samples, q)) - 1e-9))
        inbound = tuple(Inbound(o.po_id, o.qty, o.expected_day + max(d, extra)) for o in ctx.open_pos)
        out.append(SupplyCase(w, ctx.stated_lead_time + d, inbound, f"{label}: +{d}d"))
    return out


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
        skus[ctx.sku_id] = SkuWorld(paths, std, exp, inbound, notes, ids, supply_cases(snap, ctx))
    return World("planning", skus, {"demand": "forecast low/mid/high paths (cumulative p10/p50/p90) weighted 25/50/25",
                                    "lead_time": "3 supplier-delay cases (P15/P50/P85 of historical delays, weighted "
                                                 "30/40/30), same delay for all POs of a supplier; open POs at least "
                                                 "the confirmed delay; expedite on time. Displayed arrival: stated + P80"})


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


MC_DEMAND_SHOCK_SIGMA = 0.1


def monte_carlo_world(snap: Snapshot, ctxs: list[SkuContext], rng: np.random.Generator, name: str = "mc") -> World:
    """One random evaluation world around the 'actual' one: demand = Poisson(actual x per-SKU lognormal shock);
    each supplier draws one historical delay that applies to its new POs and to open POs without a known outcome
    (open POs whose actual arrival is known keep it). Expedite on time."""
    sup_delay = {sid: int(rng.choice((snap.delay_samples or {}).get(sid) or [0])) for sid in snap.suppliers}
    skus = {}
    for ctx in ctxs:
        mean = np.array(snap.eval_demand.get(ctx.sku_id, ctx.forecast.p50), dtype=float)
        path = rng.poisson(np.maximum(0.0, mean) * rng.lognormal(0.0, MC_DEMAND_SHOCK_SIGMA)).astype(float)
        d = sup_delay.get(ctx.supplier_id, 0)
        inbound = [Inbound(o.po_id, o.qty, o.expected_day + int(snap.truth_po_delays.get(o.po_id, d)))
                   for o in ctx.open_pos]
        skus[ctx.sku_id] = SkuWorld([(1.0, path)], ctx.stated_lead_time + d, ctx.expedite_lead_time, inbound)
    return World(name, skus, {"supplier_delays": sup_delay})


def monte_carlo_worlds(snap: Snapshot, ctxs: list[SkuContext], n: int, seed: int = 7) -> list[World]:
    rng = np.random.default_rng(seed)
    return [monte_carlo_world(snap, ctxs, rng, f"mc-{i}") for i in range(n)]


def without_signals(snap: Snapshot, ctxs: list[SkuContext]) -> tuple[Snapshot, list[SkuContext]]:
    """Counterfactual for the 'value of signals' ablation: confirmed demand-signal multipliers divided out of
    the forecasts and confirmed supplier-delay signals dropped. Everything else is identical."""
    out = []
    for ctx in ctxs:
        sig = [e for e in demand_events(snap, ctx.sku_id) if e.source == "signal"]
        if not sig:
            out.append(ctx)
            continue
        m, _ = fc.multipliers(snap.as_of, snap.horizon, sig)
        f = ctx.forecast
        band = ForecastBand(*(list(np.round(np.asarray(x) / m, 2)) for x in (f.p10, f.p50, f.p90)),
                            f.method, f.forecast_id, [d for d in f.drivers if "Confirmed signal" not in d])
        out.append(replace(ctx, forecast=band))
    return replace(snap, confirmed=[]), out

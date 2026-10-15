"""Alerts recomputed after each update and deduplicated by (entity, type)."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Alert, utcnow
from .planner import position
from .world import SkuContext, World, defer, evaluate

OVERSTOCK_DAYS = 90
RELIABILITY_THRESHOLD = 0.8


def compute_alerts(ctxs: list[SkuContext], world: World, as_of: date) -> list[dict]:
    out: list[dict] = []
    seen_suppliers: set[str] = set()
    for ctx in ctxs:
        sw = world.skus[ctx.sku_id]
        p50 = next((p for w, p in sw.demand_paths if w >= 0.5), sw.demand_paths[0][1])
        mid = replace(sw, demand_paths=[(1.0, p50)], supply_cases=[])  # median demand, nominal (P80) supply
        o = evaluate(ctx, defer(ctx), mid)
        # Stockout dates use the full planning world (forecast range and supplier-delay cases), like the plan.
        full = evaluate(ctx, defer(ctx), sw)
        so = full.first_stockout_day
        arrivals = sorted(i.arrival_day for i in sw.open_inbound if i.arrival_day is not None)
        if so is not None:
            # Any run-out counts, including one after an earlier open PO has already arrived.
            next_arrival = next((a for a in arrivals if a > so), None)
            lt = sw.standard_arrival_day
            sev = "high" if lt is None or so < lt else "medium"
            out.append({
                "entity_id": ctx.sku_id, "alert_type": "stockout_before_arrival", "severity": sev,
                "message": f"{ctx.name}: stockout expected {as_of + timedelta(days=so)}"
                           + (f", next inbound {as_of + timedelta(days=next_arrival)}" if next_arrival is not None
                              else ", no inbound scheduled"),
                "details": {"first_stockout_day": so, "next_arrival_day": next_arrival,
                            "units_short": round(full.units_short, 1), "lead_time_days": lt},
            })
        lot_ids = {l.lot_id for l in ctx.lots}
        risky = {k: round(v, 1) for k, v in o.expired_by_lot.items() if k in lot_ids and v > 0.5}
        if risky:
            units = sum(risky.values())
            out.append({
                "entity_id": ctx.sku_id, "alert_type": "expiry", "severity": "high" if units * ctx.econ.unit_cost > 100_000 else "medium",
                "message": f"{ctx.name}: {units:.0f} units in {', '.join(risky)} exceed expected sell-through before expiry",
                "details": {"lots": risky, "writeoff_inr": round(units * ctx.econ.unit_cost, 2)},
            })
        d = float(p50.mean())
        if d > 0:
            dos = position(ctx, sw, len(p50)) / d
            if dos > OVERSTOCK_DAYS:
                out.append({
                    "entity_id": ctx.sku_id, "alert_type": "overstock", "severity": "low",
                    "message": f"{ctx.name}: {dos:.0f} days of supply (threshold {OVERSTOCK_DAYS})",
                    "details": {"days_of_supply": round(dos, 1)},
                })
        m = ctx.metrics
        if ctx.supplier_id not in seen_suppliers and m.n_receipts and m.on_time_rate < RELIABILITY_THRESHOLD:
            seen_suppliers.add(ctx.supplier_id)
            out.append({
                "entity_id": ctx.supplier_id, "alert_type": "supplier_reliability", "severity": "medium",
                "message": f"Supplier {ctx.supplier_id} on-time rate {m.on_time_rate:.0%} (< {RELIABILITY_THRESHOLD:.0%}); "
                           f"P80 delay {m.p80_delay_days:g} days",
                "details": m.to_dict(),
            })
    return out


def upsert_alerts(db: Session, computed: list[dict]) -> dict:
    """Upsert by (entity_id, alert_type); deactivate alerts that no longer apply."""
    existing = {(a.entity_id, a.alert_type): a for a in db.scalars(select(Alert))}
    keep = set()
    created = updated = 0
    now = utcnow()
    for c in computed:
        key = (c["entity_id"], c["alert_type"])
        if key in keep:
            continue
        keep.add(key)
        a = existing.get(key)
        if a is None:
            a = Alert(entity_id=c["entity_id"], alert_type=c["alert_type"], first_seen_at=now)
            db.add(a)
            existing[key] = a
            created += 1
        else:
            updated += 1
        a.severity, a.message, a.details, a.active, a.updated_at = c["severity"], c["message"], c["details"], True, now
    resolved = 0
    for key, a in existing.items():
        if key not in keep and a.active:
            a.active = False
            a.updated_at = now
            resolved += 1
    db.commit()
    return {"created": created, "updated": updated, "resolved": resolved, "active": len(keep)}

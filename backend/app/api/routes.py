from __future__ import annotations

import csv
import io
from datetime import date, timedelta

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import Response
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from ..adapters import analytics, search
from ..adapters.storage import get_storage
from ..config import get_settings
from ..db import get_db
from ..llm.client import get_llm, persist_records
from ..models import Alert, AppState, Forecast, Job, LLMCall, Message, Plan, Product, Sale, SignalEvent, utcnow
from ..schemas import DecisionIn, MessageIn, PlanIn, ReforecastIn, ScenarioIn
from ..services import plans as plan_svc
from ..services import signals as sig_svc
from ..services import state
from ..services.planner import position
from ..services.supply import reorder_math

router = APIRouter()


def _sku_or_404(snap, sku_id: str):
    if sku_id not in snap.products:
        raise HTTPException(404, "unknown SKU")
    return snap.products[sku_id]


# ---------------------------------------------------------------- health / overview

@router.get("/health")
def health(db: Session = Depends(get_db)):
    db.execute(text("SELECT 1"))
    s = get_settings()
    return {"status": "ok", "db": db.get_bind().dialect.name, "llm_configured": s.llm_configured,
            "llm_model": s.llm_model if s.llm_configured else None}


@router.get("/api/overview")
def overview(db: Session = Depends(get_db)):
    snap = state.load_snapshot(db)
    fresh = state.freshness(snap)
    alerts = db.scalars(select(Alert).where(Alert.active.is_(True)).order_by(Alert.severity, Alert.entity_id)).all()
    jobs = db.scalars(select(Job).where(Job.status.in_(["queued", "running"]))).all()
    return {
        "as_of": snap.as_of.isoformat(),
        "default_budget": get_settings().default_budget_inr,
        "n_skus": len(snap.products),
        "forecasts": {"fresh": sum(1 for v in fresh.values() if v["fresh"]), "total": len(fresh),
                      "llm": sum(1 for v in fresh.values() if v["method"] == "llm"),
                      "fallback": sum(1 for v in fresh.values() if v["method"] == "fallback"),
                      "latest": max((v["created_at"] for v in fresh.values() if v["created_at"]), default=None)},
        "pending_signals": db.scalar(select(func.count()).select_from(SignalEvent).where(SignalEvent.status == "pending")),
        "alerts": [_alert(a) for a in alerts],
        "active_jobs": [_job(j) for j in jobs],
        "llm_configured": get_settings().llm_configured,
    }


def _alert(a: Alert) -> dict:
    return {"id": a.id, "entity_id": a.entity_id, "alert_type": a.alert_type, "severity": a.severity,
            "message": a.message, "details": a.details, "first_seen_at": a.first_seen_at.isoformat(),
            "updated_at": a.updated_at.isoformat()}


def _job(j: Job) -> dict:
    return {"id": j.id, "kind": j.kind, "status": j.status, "payload": j.payload, "result": j.result, "error": j.error,
            "created_at": j.created_at.isoformat(), "finished_at": j.finished_at.isoformat() if j.finished_at else None}


@router.get("/api/alerts")
def list_alerts(db: Session = Depends(get_db)):
    return [_alert(a) for a in db.scalars(select(Alert).where(Alert.active.is_(True)).order_by(Alert.id))]


# ---------------------------------------------------------------- SKUs

@router.get("/api/skus")
def list_skus(db: Session = Depends(get_db)):
    snap = state.load_snapshot(db)
    fresh = state.freshness(snap)
    out = []
    for sku, p in snap.products.items():
        on_hand = sum(l.qty for l in snap.lots.get(sku, []))
        avg = float(snap.history[sku][-28:].mean())
        out.append({"sku_id": sku, "name": p.name, "category": p.category, "supplier_id": p.supplier_id,
                    "on_hand": on_hand, "open_po_qty": sum(o.qty for o in snap.open_pos.get(sku, [])),
                    "avg_daily_28d": round(avg, 2), "days_of_cover": round(on_hand / avg, 1) if avg else None,
                    "forecast_method": fresh[sku]["method"], "forecast_fresh": fresh[sku]["fresh"]})
    return out


@router.get("/api/skus/{sku_id}")
def sku_detail(sku_id: str, db: Session = Depends(get_db)):
    snap = state.load_snapshot(db)
    p = _sku_or_404(snap, sku_id)
    ctxs = {c.sku_id: c for c in state.build_contexts(snap)}
    ctx = ctxs[sku_id]
    pw = state.planning_world(snap, list(ctxs.values()))
    sw = pw.skus[sku_id]
    lt = sw.standard_arrival_day if sw.standard_arrival_day is not None else ctx.metrics.effective_lead_time_days
    rm = reorder_math(ctx.forecast.p50, list(ctx.history), ctx.metrics, get_settings().service_level_z, p.moq,
                      p.case_pack, position(ctx, sw, snap.horizon), lead_time_override=lt)
    f = snap.forecasts.get(sku_id)
    hist = snap.history[sku_id][-56:]
    hist_start = snap.as_of - timedelta(days=len(hist))
    sup = snap.suppliers[p.supplier_id]
    alerts = db.scalars(select(Alert).where(Alert.active.is_(True), Alert.entity_id.in_([sku_id, p.supplier_id]))).all()
    return {
        "sku_id": sku_id, "name": p.name, "category": p.category, "unit_cost": p.unit_cost, "unit_price": p.unit_price,
        "case_pack": p.case_pack, "moq": p.moq, "shelf_life_days": p.shelf_life_days,
        "as_of": snap.as_of.isoformat(),
        "history": [{"date": (hist_start + timedelta(days=i)).isoformat(), "units": float(v)} for i, v in enumerate(hist)],
        "forecast": {
            "id": ctx.forecast.forecast_id, "method": ctx.forecast.method,
            "fallback_reason": f.fallback_reason if f else "no stored forecast; computed seasonal-naive on the fly",
            "fresh": state.freshness(snap)[sku_id]["fresh"],
            "created_at": f.created_at.isoformat() if f else None,
            "drivers": ctx.forecast.drivers,
            "days": [{"date": (snap.as_of + timedelta(days=i)).isoformat(), "p10": ctx.forecast.p10[i],
                      "p50": ctx.forecast.p50[i], "p90": ctx.forecast.p90[i]} for i in range(snap.horizon)],
        },
        "lots": [{"lot_id": l.lot_id, "qty": l.qty, "received_date": l.received_date.isoformat(),
                  "expiry_date": l.expiry_date.isoformat(), "days_to_expiry": (l.expiry_date - snap.as_of).days}
                 for l in snap.lots.get(sku_id, [])],
        "open_pos": [{"po_id": o.po_id, "qty": o.qty, "order_date": o.order_date.isoformat(),
                      "expected_date": o.expected_date.isoformat()} for o in snap.open_pos.get(sku_id, [])],
        "supplier": {"supplier_id": sup.supplier_id, "name": sup.name, "terms": sup.terms,
                     "expedite_lead_time_days": sup.expedite_lead_time_days,
                     "expedite_premium_pct": sup.expedite_premium_pct, **ctx.metrics.to_dict()},
        "reorder": {**rm.to_dict(), "planning_lead_time_days": lt, "notes": sw.notes,
                    "formula": "SS = z * sqrt(LT * sigma_d^2 + d^2 * sigma_LT^2); ROP = d * LT + SS"},
        "alerts": [_alert(a) for a in alerts],
    }


@router.get("/api/search")
def search_api(q: str = Query(..., min_length=1, max_length=100), db: Session = Depends(get_db)):
    return {"products": search.search_products(db, q), "suppliers": search.search_suppliers(db, q)}


# ---------------------------------------------------------------- signals

@router.get("/api/messages/demo")
def demo_messages():
    d = state.get_settings().data_dir
    from pathlib import Path

    out = []
    for f in sorted(Path(d, "messages").glob("*.txt")):
        out.append({"name": f.stem, "text": f.read_text(encoding="utf-8")})
    return out


def _ingest(db: Session, text_: str, source: str, storage_key: str | None = None) -> dict:
    snap = state.load_snapshot(db)
    catalog = [{"sku_id": p.sku_id, "name": p.name, "category": p.category, "supplier_id": p.supplier_id}
               for p in snap.products.values()]
    suppliers = [{"supplier_id": s.supplier_id, "name": s.name} for s in snap.suppliers.values()]
    open_pos = [{"po_id": o.po_id, "sku_id": o.sku_id, "supplier_id": o.supplier_id,
                 "expected_date": o.expected_date.isoformat()} for v in snap.open_pos.values() for o in v]
    lots = [{"lot_id": l.lot_id, "sku_id": l.sku_id, "expiry_date": l.expiry_date.isoformat()}
            for v in snap.lots.values() for l in v]
    method, results, rec = sig_svc.extract(get_llm(), text_, catalog, suppliers, open_pos, lots, snap.as_of)
    msg = Message(source=source, text=text_, storage_key=storage_key, extraction_method=method)
    db.add(msg)
    db.flush()
    if rec.status != "unavailable":
        persist_records(db, [rec])
    for ev, raw, reason in results:
        db.add(SignalEvent(
            message_id=msg.id,
            event_type=(ev.event_type if ev else str(raw.get("event_type", "other"))[:30]),
            sku_ids=(ev.sku_ids if ev else []), magnitude=(ev.magnitude if ev else 0.0),
            start_date=ev.start_date if ev else None, end_date=ev.end_date if ev else None,
            quoted_evidence=(ev.quoted_evidence if ev else str(raw.get("quoted_evidence", ""))[:1000]),
            status="pending" if reason is None else "invalid", rejection_reason=reason,
        ))
    db.commit()
    return {"message_id": msg.id, "extraction_method": method,
            "llm_error": rec.error if method != "llm" else None,
            "events": [_signal(e) for e in db.scalars(select(SignalEvent).where(SignalEvent.message_id == msg.id))]}


@router.post("/api/messages")
def post_message(body: MessageIn, db: Session = Depends(get_db)):
    key = get_storage().put(f"messages/{utcnow().strftime('%Y%m%dT%H%M%S%f')}.txt", body.text.encode("utf-8"))
    return _ingest(db, body.text, body.source, key)


@router.post("/api/messages/upload")
async def upload_message(file: UploadFile = File(...), db: Session = Depends(get_db)):
    data = await file.read(get_settings().max_message_chars * 4 + 1)
    if len(data) > get_settings().max_message_chars * 4:
        raise HTTPException(413, "message file too large")
    try:
        t = data.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(400, "message must be UTF-8 text")
    if not t.strip() or len(t) > get_settings().max_message_chars:
        raise HTTPException(400, "message empty or too long")
    key = get_storage().put(f"messages/{utcnow().strftime('%Y%m%dT%H%M%S%f')}.txt", data)
    return _ingest(db, t, "upload", key)


def _signal(e: SignalEvent) -> dict:
    return {"id": e.id, "ref": f"EV-{e.id}", "message_id": e.message_id, "event_type": e.event_type,
            "sku_ids": e.sku_ids, "magnitude": e.magnitude,
            "start_date": e.start_date.isoformat() if e.start_date else None,
            "end_date": e.end_date.isoformat() if e.end_date else None, "quoted_evidence": e.quoted_evidence,
            "status": e.status, "rejection_reason": e.rejection_reason, "created_at": e.created_at.isoformat()}


@router.get("/api/signals")
def list_signals(status: str | None = None, db: Session = Depends(get_db)):
    q = select(SignalEvent).order_by(SignalEvent.id.desc())
    if status:
        q = q.where(SignalEvent.status == status)
    out = []
    for e in db.scalars(q.limit(200)):
        out.append(_signal(e))
    return out


def _decide(db: Session, signal_id: int, new_status: str, reason: str | None, bg: BackgroundTasks) -> dict:
    e = db.get(SignalEvent, signal_id)
    if e is None:
        raise HTTPException(404, "signal not found")
    if e.status == "invalid":
        raise HTTPException(409, f"signal failed validation: {e.rejection_reason}")
    was_confirmed = e.status == "confirmed"
    e.status, e.decided_at = new_status, utcnow()
    if reason:
        e.rejection_reason = reason
    db.commit()
    job = None
    if new_status == "confirmed" or was_confirmed:
        job = plan_svc.enqueue(db, "reforecast", {"sku_ids": e.sku_ids, "trigger": f"EV-{e.id} {new_status}"})
        bg.add_task(plan_svc.run_job, job.id)
    return {"signal": _signal(e), "job": _job(job) if job else None}


@router.post("/api/signals/{signal_id}/confirm")
def confirm_signal(signal_id: int, bg: BackgroundTasks, db: Session = Depends(get_db)):
    return _decide(db, signal_id, "confirmed", None, bg)


@router.post("/api/signals/{signal_id}/reject")
def reject_signal(signal_id: int, bg: BackgroundTasks, body: DecisionIn | None = None, db: Session = Depends(get_db)):
    return _decide(db, signal_id, "rejected", body.reason if body else None, bg)


# ---------------------------------------------------------------- forecasts / jobs

@router.post("/api/forecasts/refresh")
def refresh(body: ReforecastIn, bg: BackgroundTasks, db: Session = Depends(get_db)):
    job = plan_svc.enqueue(db, "reforecast", {"sku_ids": body.sku_ids, "force": body.force, "trigger": "manual"})
    bg.add_task(plan_svc.run_job, job.id)
    return _job(job)


@router.get("/api/jobs/{job_id}")
def get_job(job_id: int, db: Session = Depends(get_db)):
    j = db.get(Job, job_id)
    if j is None:
        raise HTTPException(404, "job not found")
    return _job(j)


@router.get("/api/jobs")
def list_jobs(db: Session = Depends(get_db)):
    return [_job(j) for j in db.scalars(select(Job).order_by(Job.id.desc()).limit(20))]


@router.get("/api/llm/calls")
def llm_calls(db: Session = Depends(get_db)):
    return [{"id": c.id, "task": c.task, "input_hash": c.input_hash[:12], "model": c.model, "status": c.status,
             "error": c.error, "latency_ms": c.latency_ms, "created_at": c.created_at.isoformat()}
            for c in db.scalars(select(LLMCall).order_by(LLMCall.id.desc()).limit(50))]


# ---------------------------------------------------------------- CSV import

SALES_COLUMNS = {"sale_date", "sku_id", "channel", "segment", "units"}


@router.post("/api/import/sales")
async def import_sales(bg: BackgroundTasks, file: UploadFile = File(...), db: Session = Depends(get_db)):
    limit = get_settings().max_csv_bytes
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(413, f"CSV larger than {limit} bytes")
    try:
        reader = csv.DictReader(io.StringIO(data.decode("utf-8-sig")))
    except UnicodeDecodeError:
        raise HTTPException(400, "CSV must be UTF-8")
    if not reader.fieldnames or not SALES_COLUMNS.issubset({c.strip() for c in reader.fieldnames}):
        raise HTTPException(400, f"CSV must have columns {sorted(SALES_COLUMNS)}")
    known = set(db.scalars(select(Product.sku_id)))
    rows, errors = [], []
    for i, r in enumerate(reader, start=2):
        try:
            sku = r["sku_id"].strip().upper()
            if sku not in known:
                raise ValueError(f"unknown SKU {sku}")
            units = int(r["units"])
            if units < 0 or units > 100000:
                raise ValueError("units out of range")
            rows.append(Sale(sale_date=date.fromisoformat(r["sale_date"].strip()), sku_id=sku,
                             channel=r["channel"].strip()[:40], segment=r["segment"].strip()[:40], units=units))
        except (ValueError, KeyError, AttributeError) as exc:
            errors.append({"line": i, "error": str(exc)[:200]})
        if len(errors) > 50:
            break
    if errors:
        raise HTTPException(400, {"message": "CSV rejected; nothing imported", "errors": errors[:50]})
    db.add_all(rows)
    db.commit()
    get_storage().put(f"uploads/sales_{utcnow().strftime('%Y%m%dT%H%M%S%f')}.csv", data)
    skus = sorted({r.sku_id for r in rows})
    job = plan_svc.enqueue(db, "reforecast", {"sku_ids": None, "trigger": f"CSV import ({len(rows)} rows)"})
    bg.add_task(plan_svc.run_job, job.id)
    return {"imported": len(rows), "skus": skus, "job": _job(job)}


# ---------------------------------------------------------------- plans

def _plan(p: Plan) -> dict:
    return {"id": p.id, "as_of": p.as_of.isoformat(), "budget": p.budget, "status": p.status, "actions": p.actions,
            "baseline_actions": p.baseline_actions, "summary": p.summary, "explanation": p.explanation,
            "exports": p.exports, "created_at": p.created_at.isoformat(),
            "approved_at": p.approved_at.isoformat() if p.approved_at else None}


@router.post("/api/plans")
def create_plan(body: PlanIn, db: Session = Depends(get_db)):
    return _plan(plan_svc.create_plan(db, body.budget))


@router.get("/api/plans/latest")
def latest_plan(db: Session = Depends(get_db)):
    p = db.scalars(select(Plan).order_by(Plan.id.desc()).limit(1)).first()
    if p is None:
        raise HTTPException(404, "no plan yet")
    return _plan(p)


@router.get("/api/plans/{plan_id}")
def get_plan(plan_id: int, db: Session = Depends(get_db)):
    p = db.get(Plan, plan_id)
    if p is None:
        raise HTTPException(404, "plan not found")
    return _plan(p)


@router.post("/api/plans/{plan_id}/approve")
def approve(plan_id: int, db: Session = Depends(get_db)):
    p = db.get(Plan, plan_id)
    if p is None:
        raise HTTPException(404, "plan not found")
    if p.status == "approved":
        return _plan(p)
    return _plan(plan_svc.approve_plan(db, p))


@router.get("/api/plans/{plan_id}/export/{kind}")
def export(plan_id: int, kind: str, db: Session = Depends(get_db)):
    p = db.get(Plan, plan_id)
    if p is None or not p.exports:
        raise HTTPException(404, "plan not approved")
    key = {"po": p.exports.get("po_csv"), "markdowns": p.exports.get("markdown_csv")}.get(kind)
    if not key:
        raise HTTPException(404, "unknown export")
    return Response(get_storage().get(key), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{key.split("/")[-1]}"'})


# ---------------------------------------------------------------- scenario / value

@router.post("/api/scenarios")
def scenario(body: ScenarioIn, db: Session = Depends(get_db)):
    try:
        return plan_svc.run_scenario(db, body.question, body.budget)
    except ValueError as exc:
        raise HTTPException(400, f"could not understand the scenario: {str(exc)[:300]}")


@router.get("/api/backtest")
def get_backtest(db: Session = Depends(get_db)):
    row = db.get(AppState, "backtest")
    return row.value if row else None


@router.post("/api/backtest")
def start_backtest(bg: BackgroundTasks, db: Session = Depends(get_db)):
    job = plan_svc.enqueue(db, "backtest", {})
    bg.add_task(plan_svc.run_job, job.id)
    return _job(job)


@router.post("/api/analytics/export")
def analytics_export(db: Session = Depends(get_db)):
    return {"keys": analytics.export_tables(db, state.get_as_of(db))}


@router.get("/api/analytics/sales")
def analytics_sales(days: int = Query(28, ge=1, le=365), db: Session = Depends(get_db)):
    return analytics.sales_by_channel_segment(db, state.get_as_of(db) - timedelta(days=days))


@router.get("/api/forecasts/{sku_id}/history")
def forecast_history(sku_id: str, db: Session = Depends(get_db)):
    return [{"id": f.id, "as_of": f.as_of.isoformat(), "method": f.method, "created_at": f.created_at.isoformat(),
             "fallback_reason": f.fallback_reason}
            for f in db.scalars(select(Forecast).where(Forecast.sku_id == sku_id).order_by(Forecast.id.desc()).limit(20))]

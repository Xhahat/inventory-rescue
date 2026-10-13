"""Plan lifecycle: build (planner + baseline + scorecard + explanation), approve (PO CSV + markdown list),
scenario what-ifs, backtest. Also background jobs for continuous sensing."""

from __future__ import annotations

import csv
import io
from datetime import date, timedelta

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..adapters.storage import get_storage
from ..config import get_settings
from ..db import session_factory
from ..llm.client import get_llm, persist_records
from ..models import AppState, Job, Plan, Product, Supplier, utcnow
from . import alerts as alerts_svc
from . import explanation as expl
from . import forecasting as fc
from . import scenarios as scen
from . import state
from . import value
from .planner import action_reasons, baseline_actions, build_plan
from .world import Action, SkuContext, World

KIND_LABEL = {
    "standard_po": "Buy (standard PO)",
    "expedited_po": "Expedite PO",
    "markdown": "Markdown expiring lot",
    "markdown+standard_po": "Markdown + buy",
    "markdown+expedited_po": "Markdown + expedite",
    "defer": "Defer",
}


def _action_rows(result, as_of: date) -> list[dict]:
    rows = []
    for dec in result.decisions:
        a, ctx = dec.action, dec.ctx
        if a.kind == "defer":
            continue
        rec_ids = [l.lot_id for l in ctx.lots] + [p.po_id for p in ctx.open_pos]
        rec_ids += [f"EV-{e}" for e in dec.event_ids]
        if ctx.forecast.forecast_id:
            rec_ids.append(f"FC-{ctx.forecast.forecast_id}")
        arrival = dec.outcome.arrival_day
        rows.append({
            "id": f"ACT-{ctx.sku_id}",
            "sku_id": ctx.sku_id,
            "sku_name": ctx.name,
            "category": ctx.category,
            "supplier_id": ctx.supplier_id,
            "kind": a.kind,
            "label": KIND_LABEL[a.kind],
            "order_qty": a.order_qty,
            "expedite": a.expedite,
            "arrival_date": (as_of + timedelta(days=arrival)).isoformat() if arrival is not None and a.order_qty else None,
            "markdown_lot_id": a.markdown.lot_id if a.markdown else None,
            "discount_pct": a.markdown.discount_pct if a.markdown else None,
            "cash_outlay": round(a.cash_outlay, 2),
            "expedite_premium": round(a.expedite_premium, 2),
            "expected_loss_if_deferred": round(dec.defer_outcome.total_loss, 2),
            "expected_loss_with_action": round(dec.outcome.total_loss, 2),
            "loss_avoided": round(dec.loss_avoided, 2),
            "forecast_method": ctx.forecast.method,
            "reasons": action_reasons(dec, as_of),
            "record_ids": rec_ids,
            "raw": a.to_dict(),
        })
    rows.sort(key=lambda r: -r["loss_avoided"])
    for i, r in enumerate(rows, 1):
        r["rank"] = i
    return rows


def explanation_input(plan_summary: dict, rows: list[dict]) -> dict:
    kinds: dict[str, int] = {}
    cash_by_kind: dict[str, float] = {}
    avoided_by_kind: dict[str, float] = {}
    for r in rows:
        kinds[r["kind"]] = kinds.get(r["kind"], 0) + 1
        cash_by_kind[r["kind"]] = round(cash_by_kind.get(r["kind"], 0.0) + r["cash_outlay"], 2)
        avoided_by_kind[r["kind"]] = round(avoided_by_kind.get(r["kind"], 0.0) + r["loss_avoided"], 2)
    out = {
        "as_of": plan_summary["as_of"],
        "horizon_days": get_settings().horizon_days,
        "budget": plan_summary["budget"],
        "cash_used": plan_summary["cash_used"],
        "cash_remaining": plan_summary.get("cash_remaining"),
        "expected_loss_no_action": plan_summary["expected_loss_no_action"],
        "expected_loss_plan": plan_summary["expected_loss_plan"],
        "expected_loss_avoided": round(plan_summary["expected_loss_no_action"] - plan_summary["expected_loss_plan"], 2),
        "n_actions": len(rows),
        "actions_by_kind": kinds,
        "cash_by_kind": cash_by_kind,
        "loss_avoided_by_kind": avoided_by_kind,
        "orders_cash_total": round(sum(r["cash_outlay"] for r in rows if r["order_qty"]), 2),
        "orders_units_total": sum(r["order_qty"] for r in rows),
        "loss_avoided_total": round(sum(r["loss_avoided"] for r in rows), 2),
        "actions": [{"rank": i + 1, **{k: r[k] for k in ("id", "sku_name", "kind", "order_qty", "arrival_date",
                                                          "markdown_lot_id", "discount_pct", "cash_outlay",
                                                          "loss_avoided", "reasons", "record_ids")}}
                    for i, r in enumerate(rows)],
    }
    card = plan_summary.get("scorecard")
    if card:
        out["vs_rule_baseline_simulated"] = {"plan_total_loss": card["plan"]["total_loss"],
                                             "baseline_total_loss": card["baseline"]["total_loss"],
                                             "savings": card["savings"]}
    return out


def _plan_core(snap, ctxs: list[SkuContext], pw: World, ew: World, budget: float):
    s = get_settings()
    result = build_plan(ctxs, pw, budget, s.service_level_z)
    base = baseline_actions(ctxs, budget)
    rows = _action_rows(result, snap.as_of)
    card = value.compare(ctxs, result.actions, base, ew, include_per_sku=True)
    sens = value.sensitivity(ctxs, result.actions, base, ew)
    summary = {
        "as_of": snap.as_of.isoformat(),
        "budget": round(budget, 2),
        "cash_used": round(result.cash_used, 2),
        "cash_remaining": round(budget - result.cash_used, 2),
        "expected_loss_no_action": round(result.expected_loss_no_action, 2),
        "expected_loss_plan": round(result.expected_loss_plan, 2),
        "n_actions": len(rows),
        "skus_at_risk": sum(1 for d in result.decisions if d.defer_outcome.total_loss > 1),
        "forecast_methods": {m: sum(1 for c in ctxs if c.forecast.method == m) for m in ("llm", "fallback")},
        "scorecard": {k: v for k, v in card.items() if k != "per_sku"},
        "scorecard_per_sku": card["per_sku"],
        "sensitivity": sens,
        "evaluation_world": ew.description,
        "planning_world": pw.description,
    }
    return result, base, rows, summary


def create_plan(db: Session, budget: float | None = None) -> Plan:
    s = get_settings()
    budget = float(s.default_budget_inr if budget is None else budget)
    snap = state.load_snapshot(db)
    ctxs = state.build_contexts(snap)
    pw = state.planning_world(snap, ctxs)
    ew = state.evaluation_world(snap, ctxs)
    result, base, rows, summary = _plan_core(snap, ctxs, pw, ew, budget)
    method, exp, rec, err = expl.explain(get_llm(), explanation_input(summary, rows))
    if rec.status != "unavailable":
        persist_records(db, [rec])
    plan = Plan(as_of=snap.as_of, budget=budget, status="draft", actions=rows,
                baseline_actions=[a.to_dict() for a in base if a.kind != "defer"], summary=summary,
                explanation={"method": method, "error": err, **exp.model_dump()})
    db.add(plan)
    db.commit()
    alerts_svc.upsert_alerts(db, alerts_svc.compute_alerts(ctxs, pw, snap.as_of))
    return plan


def approve_plan(db: Session, plan: Plan) -> Plan:
    st = get_storage()
    products = {p.sku_id: p for p in db.scalars(select(Product))}
    suppliers = {x.supplier_id: x for x in db.scalars(select(Supplier))}
    po_buf, md_buf = io.StringIO(), io.StringIO()
    po = csv.writer(po_buf)
    po.writerow(["draft_po_id", "supplier_id", "supplier_name", "sku_id", "sku_name", "qty", "unit_cost_inr",
                 "line_total_inr", "expedite", "expedite_premium_inr", "expected_arrival", "plan_id"])
    md = csv.writer(md_buf)
    md.writerow(["sku_id", "sku_name", "lot_id", "discount_pct", "list_price_inr", "markdown_price_inr", "plan_id"])
    n_po = n_md = 0
    for r in plan.actions:
        p = products[r["sku_id"]]
        if r["order_qty"]:
            n_po += 1
            po.writerow([f"DPO-{plan.id}-{n_po:03d}", p.supplier_id, suppliers[p.supplier_id].name, p.sku_id, p.name,
                         r["order_qty"], p.unit_cost, round(r["order_qty"] * p.unit_cost, 2), r["expedite"],
                         r["expedite_premium"], r["arrival_date"], plan.id])
        if r.get("markdown_lot_id"):
            n_md += 1
            md.writerow([p.sku_id, p.name, r["markdown_lot_id"], r["discount_pct"], p.unit_price,
                         round(p.unit_price * (1 - r["discount_pct"]), 2), plan.id])
    po_key = st.put(f"exports/plan_{plan.id}_draft_po.csv", po_buf.getvalue().encode("utf-8"))
    md_key = st.put(f"exports/plan_{plan.id}_markdowns.csv", md_buf.getvalue().encode("utf-8"))
    plan.status = "approved"
    plan.approved_at = utcnow()
    plan.exports = {"po_csv": po_key, "markdown_csv": md_key, "po_lines": n_po, "markdown_lines": n_md}
    db.commit()
    return plan


# ---------------------------------------------------------------- scenarios

def _catalog(snap) -> tuple[list[dict], list[dict]]:
    cat = [{"sku_id": p.sku_id, "name": p.name, "category": p.category, "supplier_id": p.supplier_id}
           for p in snap.products.values()]
    sups = [{"supplier_id": s.supplier_id, "name": s.name} for s in snap.suppliers.values()]
    return cat, sups


def run_scenario(db: Session, question: str, budget: float | None) -> dict:
    s = get_settings()
    snap = state.load_snapshot(db)
    ctxs = state.build_contexts(snap)
    cat, sups = _catalog(snap)
    method, spec, rec = scen.parse(get_llm(), question, cat, sups)
    if rec.status != "unavailable":
        persist_records(db, [rec])
        db.commit()
    base_budget = float(budget if budget is not None else s.default_budget_inr)
    bud = float(spec.budget if spec.budget is not None else base_budget)
    pw = state.planning_world(snap, ctxs)
    ew = state.evaluation_world(snap, ctxs)
    current = build_plan(ctxs, pw, base_budget, s.service_level_z)
    spw, sew = scen.apply(pw, ctxs, spec), scen.apply(ew, ctxs, spec)
    adapted = build_plan(ctxs, spw, bud, s.service_level_z)
    base = baseline_actions(ctxs, bud)
    c_cur = value.evaluate_actions(ctxs, current.actions, sew)["totals"]
    c_new = value.evaluate_actions(ctxs, adapted.actions, sew)["totals"]
    c_base = value.evaluate_actions(ctxs, base, sew)["totals"]
    changed = []
    cur_by = {a.sku_id: a for a in current.actions}
    for dec in adapted.decisions:
        a, b = dec.action, cur_by[dec.ctx.sku_id]
        if (a.kind, a.order_qty, a.markdown) != (b.kind, b.order_qty, b.markdown):
            changed.append({"sku_id": a.sku_id, "sku_name": dec.ctx.name,
                            "before": f"{KIND_LABEL[b.kind]}" + (f" {b.order_qty}" if b.order_qty else ""),
                            "after": f"{KIND_LABEL[a.kind]}" + (f" {a.order_qty}" if a.order_qty else "")})
    return {
        "parse_method": method,
        "parse_error": rec.error if method != "llm" else None,
        "spec": spec.model_dump(),
        "budget": bud,
        "current_budget": base_budget,
        "current_cash_used": round(current.cash_used, 2),
        "outcomes_in_scenario": {"current_plan": c_cur, "adapted_plan": c_new, "baseline": c_base},
        "adapted_cash_used": round(adapted.cash_used, 2),
        "changed_actions": changed,
        "adapted_actions": [{"sku_id": a.sku_id, **a.to_dict()} for a in adapted.actions if a.kind != "defer"],
    }


# ---------------------------------------------------------------- backtest

BACKTEST_DAYS = 14
BACKTEST_SKUS = 10


def run_backtest(db: Session) -> dict:
    """Hold out the last 14 days of sales; compare LLM vs seasonal-naive WAPE on the top-volume SKUs."""
    snap = state.load_snapshot(db)
    llm = get_llm()
    as_of = snap.as_of - timedelta(days=BACKTEST_DAYS)
    vols = sorted(snap.products, key=lambda k: -float(snap.history[k][-56:].sum()))[:BACKTEST_SKUS]
    jobs, actual = [], {}
    for sku in vols:
        hist = snap.history[sku][:-BACKTEST_DAYS]
        actual[sku] = snap.history[sku][-BACKTEST_DAYS:]
        p = snap.products[sku]
        rows = [r for r in state.context_rows(snap, sku) if r["start"] < snap.as_of.isoformat()]
        evs = [e for e in state.demand_events(snap, sku) if not isinstance(e.id, int) and e.start < snap.as_of]
        summary = fc.build_summary({"sku_id": sku, "name": p.name, "category": p.category}, hist, as_of,
                                   BACKTEST_DAYS, evs, rows)
        jobs.append({"sku_id": sku, "summary": summary, "history": hist, "as_of": as_of, "horizon": BACKTEST_DAYS,
                     "events": evs, "hash": fc.forecast_hash(summary, llm.model if llm else "none")})
    outs = fc.forecast_many(llm, jobs, get_settings().llm_max_concurrency)
    per_sku, a_all, l_all, n_all = [], [], [], []
    llm_ok = 0
    for j, o in zip(jobs, outs):
        p10, naive, p90, _ = fc.fallback_forecast(j["history"], as_of, BACKTEST_DAYS, j["events"])
        a = actual[j["sku_id"]]
        per_sku.append({"sku_id": j["sku_id"], "llm_method": o.method,
                        "wape_llm": None if o.method != "llm" else round(value.wape(a, o.p50) or 0, 4),
                        "wape_naive": round(value.wape(a, naive) or 0, 4)})
        persist_records(db, [r for r in o.records if r.status != "unavailable"])
        if o.method == "llm":
            llm_ok += 1
            a_all.extend(a)
            l_all.extend(o.p50)
            n_all.extend(naive)
    naive_all_a = np.concatenate([actual[j["sku_id"]] for j in jobs]) if jobs else np.array([])
    naive_all_f = np.concatenate([fc.fallback_forecast(j["history"], as_of, BACKTEST_DAYS, j["events"])[1] for j in jobs]) if jobs else np.array([])
    result = {
        "holdout_start": as_of.isoformat(),
        "holdout_days": BACKTEST_DAYS,
        "skus": len(jobs),
        "llm_valid_forecasts": llm_ok,
        "wape_naive_all": round(value.wape(naive_all_a, naive_all_f) or 0, 4),
        "wape_llm_on_valid": round(value.wape(a_all, l_all) or 0, 4) if llm_ok else None,
        "wape_naive_on_valid": round(value.wape(a_all, n_all) or 0, 4) if llm_ok else None,
        "per_sku": per_sku,
        "computed_at": utcnow().isoformat(),
        "note": "WAPE = sum|actual - forecast| / sum actual over the hold-out window (lower is better).",
    }
    row = db.get(AppState, "backtest")
    if row is None:
        db.add(AppState(key="backtest", value=result))
    else:
        row.value = result
    db.commit()
    return result


# ---------------------------------------------------------------- jobs (continuous sensing)

def enqueue(db: Session, kind: str, payload: dict) -> Job:
    job = Job(kind=kind, payload=payload, status="queued")
    db.add(job)
    db.commit()
    return job


def run_job(job_id: int) -> None:
    db = session_factory()()
    try:
        job = db.get(Job, job_id)
        if job is None:
            return
        job.status = "running"
        db.commit()
        try:
            if job.kind == "reforecast":
                res = state.refresh_forecasts(db, job.payload.get("sku_ids"), bool(job.payload.get("force")))
                recompute_alerts(db)
            elif job.kind == "backtest":
                res = run_backtest(db)
            else:
                raise ValueError(f"unknown job kind {job.kind}")
            job.result, job.status = res, "done"
        except Exception as exc:  # recorded on the job row
            db.rollback()
            job = db.get(Job, job_id)
            job.status, job.error = "failed", f"{type(exc).__name__}: {exc}"[:2000]
        job.finished_at = utcnow()
        db.commit()
    finally:
        db.close()


def recompute_alerts(db: Session) -> dict:
    snap = state.load_snapshot(db)
    ctxs = state.build_contexts(snap)
    return alerts_svc.upsert_alerts(db, alerts_svc.compute_alerts(ctxs, state.planning_world(snap, ctxs), snap.as_of))

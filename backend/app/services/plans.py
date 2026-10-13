"""Plan lifecycle: build (planner + baseline + scorecard + explanation), approve (PO CSV + markdown list),
scenario what-ifs, backtest. Also background jobs for continuous sensing."""

from __future__ import annotations

import csv
import io
from datetime import date, timedelta

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
from . import rolling
from . import scenarios as scen
from . import state
from . import value
from .planner import (action_reasons, baseline_actions, build_plan, prepare_menu, score_actions, solve_menu,
                      tuned_baseline)
from .simulator import Markdown
from .supply import round_order_qty
from .world import Action, SkuContext, World, defer

KIND_LABEL = {
    "standard_po": "Buy (standard PO)",
    "expedited_po": "Expedite PO",
    "staggered_po": "Staggered buy",
    "markdown": "Markdown expiring lot",
    "markdown+standard_po": "Markdown + buy",
    "markdown+expedited_po": "Markdown + expedite",
    "defer": "Defer",
}

STRATEGIES = {
    "commit_now": ("Commit now", "Best plan if every purchase is placed today (the classic one-shot plan)."),
    "staggered": ("Staggered buys", "Split larger buys: part today, the rest ordered on the day 7 or 14 review "
                                    "and re-confirmed then. Optimised with a working-capital charge, so later "
                                    "tranches are preferred when they cost little or nothing in expected loss."),
    "reserve": ("Keep a reserve", "Spend only part of the budget today and keep the rest for the weekly "
                                  "re-plans, to react to what demand and suppliers actually do."),
    "lean": ("Lean budget", "Smallest budget that keeps most of the full plan's savings; the remaining cash "
                            "is not committed at all."),
}


def _d(as_of: date, day: int | None) -> str | None:
    return None if day is None else (as_of + timedelta(days=day)).isoformat()


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
            "later_qty": a.later_qty,
            "later_order_date": _d(as_of, a.later_day) if a.later_qty else None,
            "later_arrival_date": _d(as_of, dec.outcome.later_arrival_day) if a.later_qty else None,
            "markdown_lot_id": a.markdown.lot_id if a.markdown else None,
            "discount_pct": a.markdown.discount_pct if a.markdown else None,
            "cash_outlay": round(a.cash_outlay, 2),
            "cash_today": round(a.cash_today, 2),
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
        "orders_units_total": sum(r["order_qty"] + r.get("later_qty", 0) for r in rows),
        "cash_paid_today": plan_summary.get("cash_today"),
        "plan_option": plan_summary.get("strategy_label"),
        "loss_avoided_total": round(sum(r["loss_avoided"] for r in rows), 2),
        "actions": [{"rank": i + 1, **{k: r.get(k) for k in ("id", "sku_name", "kind", "order_qty", "arrival_date",
                                                              "later_qty", "later_order_date",
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


FRONTIER_MULTIPLES = (0.0, 0.25, 0.5, 0.75, 1.0, 1.5, 2.0)


def _frontier(ctxs, menu, ew: World, budget: float) -> list[dict]:
    rows = []
    for b in sorted({round(budget * m, 2) for m in FRONTIER_MULTIPLES} | {round(budget, 2)}):
        r = solve_menu(menu, b)
        ev = value.evaluate_actions(ctxs, r.actions, ew)
        rows.append({"budget": b, "cash_used": round(r.cash_used, 2),
                     "planning_loss": round(r.expected_loss_plan, 2),
                     "planning_objective": round(r.expected_loss_plan + r.service_penalty_plan, 2),
                     "eval_loss": ev["totals"]["total_loss"], "eval_fill_rate": ev["totals"]["fill_rate"],
                     "eval_hero_fill_rate": value.hero_fill_rate(ctxs, ev["per_sku"]),
                     "n_actions": sum(1 for a in r.actions if a.kind != "defer"), "is_current": b == round(budget, 2)})
    return rows


def _solve_strategy(menu, budget: float, strategy: str):
    """Solve the menu for one plan option. Returns (result, operating budget for the rest of the month)."""
    s = get_settings()
    if strategy == "staggered":
        return solve_menu(menu, budget, stagger=True, capital_rate=s.working_capital_rate), budget
    if strategy == "reserve":
        return solve_menu(menu, budget * (1 - s.reserve_share)), budget
    full = solve_menu(menu, budget)
    if strategy == "lean":
        def obj(r):
            return r.expected_loss_plan + r.service_penalty_plan
        defer_obj = sum(m.defer_outcome.objective for m in menu.skus)
        target = s.lean_savings_share * (defer_obj - obj(full))
        for k in range(1, 21):
            b = round(budget * k / 20, 2)
            r = full if k == 20 else solve_menu(menu, b)
            if defer_obj - obj(r) >= target - 1e-6:
                return r, b
    return full, budget


def _options(ctxs, menu, ew: World, budget: float, current: str) -> tuple[dict, list[dict]]:
    s = get_settings()
    results, rows = {}, []
    for key, (label, desc) in STRATEGIES.items():
        r, op_budget = _solve_strategy(menu, budget, key)
        results[key] = (r, op_budget)
        ev = value.evaluate_actions(ctxs, r.actions, ew)
        h = s.horizon_days
        rows.append({
            "key": key, "label": label, "description": desc, "is_current": key == current,
            "operating_budget": round(op_budget, 2),
            "cash_today": round(r.cash_today, 2),
            "cash_scheduled": round(r.cash_used - r.cash_today, 2),
            "cash_committed": round(r.cash_used, 2),
            "cash_uncommitted": round(budget - r.cash_used, 2),
            "planning_loss": round(r.expected_loss_plan, 2),
            "planning_objective": round(r.expected_loss_plan + r.service_penalty_plan, 2),
            "capital_cost": round(sum(a.capital_cost(h, s.working_capital_rate) for a in r.actions), 2),
            "eval_loss_no_review": ev["totals"]["total_loss"],
            "eval_fill_rate": ev["totals"]["fill_rate"],
            "eval_hero_fill_rate": value.hero_fill_rate(ctxs, ev["per_sku"]),
            "n_actions": sum(1 for a in r.actions if a.kind != "defer"),
            "n_staggered": sum(1 for a in r.actions if a.later_qty),
            "actions": [a.to_dict() for a in r.actions if a.kind != "defer"],
        })
    return results, rows


def _plan_core(snap, ctxs: list[SkuContext], pw: World, ew: World, budget: float,
               actions: list[Action] | None = None, strategy: str = "commit_now"):
    """Optimise (or, with `actions`, score operator-overridden decisions) and build the scorecard."""
    s = get_settings()
    z = s.service_level_z
    if strategy not in STRATEGIES:
        raise ValueError(f"unknown strategy {strategy!r}")
    menu = prepare_menu(ctxs, pw, sorted({budget * m for m in FRONTIER_MULTIPLES} | {budget}), z, stagger=True)
    opt_results, options = _options(ctxs, menu, ew, budget, strategy)
    result = opt_results[strategy][0] if actions is None else score_actions(ctxs, pw, actions, z, budget)
    base = baseline_actions(ctxs, budget)
    tuned, tuned_info = tuned_baseline(ctxs, pw, budget)
    rows = _action_rows(result, snap.as_of)
    card = value.compare(ctxs, result.actions, base, ew, include_per_sku=True)
    t_eval = value.evaluate_actions(ctxs, tuned, ew)
    card["tuned_baseline"] = t_eval["totals"]
    card["savings_vs_tuned"] = round(t_eval["totals"]["total_loss"] - card["plan"]["total_loss"], 2)
    per_sku = card["per_sku"]
    for k, v in per_sku.items():
        v["tuned_baseline"] = t_eval["per_sku"][k]
    hero = [c.sku_id for c in ctxs if c.priority == "hero"]
    service = {
        "hero_skus": hero,
        "goodwill_pct": {"hero": s.goodwill_hero_pct, "standard": s.goodwill_standard_pct},
        "plan_fill_rate": card["plan"]["fill_rate"],
        "baseline_fill_rate": card["baseline"]["fill_rate"],
        "tuned_fill_rate": card["tuned_baseline"]["fill_rate"],
        "plan_hero_fill_rate": value.hero_fill_rate(ctxs, {k: v["plan"] for k, v in per_sku.items()}),
        "baseline_hero_fill_rate": value.hero_fill_rate(ctxs, {k: v["baseline"] for k, v in per_sku.items()}),
        "tuned_hero_fill_rate": value.hero_fill_rate(ctxs, t_eval["per_sku"]),
        "planning_service_penalty": round(result.service_penalty_plan, 2),
    }
    sens = value.sensitivity(ctxs, result.actions, base, ew)
    summary = {
        "as_of": snap.as_of.isoformat(),
        "budget": round(budget, 2),
        "strategy": strategy,
        "strategy_label": STRATEGIES[strategy][0],
        "cash_used": round(result.cash_used, 2),
        "cash_today": round(result.cash_today, 2),
        "cash_scheduled": round(result.cash_used - result.cash_today, 2),
        "cash_remaining": round(budget - result.cash_used, 2),
        "expected_loss_no_action": round(result.expected_loss_no_action, 2),
        "expected_loss_plan": round(result.expected_loss_plan, 2),
        "n_actions": len(rows),
        "skus_at_risk": sum(1 for d in result.decisions if d.defer_outcome.total_loss > 1),
        "forecast_methods": {m: sum(1 for c in ctxs if c.forecast.method == m) for m in ("hybrid", "statistical")},
        "scorecard": {k: v for k, v in card.items() if k != "per_sku"},
        "scorecard_per_sku": per_sku,
        "service": service,
        "tuned_baseline": {**tuned_info, "actions": [a.to_dict() for a in tuned if a.kind != "defer"]},
        "frontier": _frontier(ctxs, menu, ew, budget),
        "options": options,
        "sensitivity": sens,
        "evaluation_world": ew.description,
        "planning_world": pw.description,
    }
    return result, base, rows, summary


DIFF_KEYS = ("kind", "order_qty", "expedite", "markdown_lot_id", "discount_pct")


def _describe(r: dict | None) -> str:
    if r is None:
        return "No action"
    parts = [KIND_LABEL.get(r["kind"], r["kind"])]
    if r.get("order_qty"):
        parts.append(f"{r['order_qty']} units")
    if r.get("discount_pct"):
        parts.append(f"{round(r['discount_pct'] * 100)}% off {r['markdown_lot_id']}")
    return ", ".join(parts)


def plan_diff(prev: Plan | None, rows: list[dict], summary: dict) -> dict | None:
    """What changed versus the previous plan, SKU by SKU."""
    if prev is None:
        return None
    old = {r["sku_id"]: r for r in prev.actions}
    new = {r["sku_id"]: r for r in rows}
    changes = []
    for sku in sorted(set(old) | set(new)):
        a, b = old.get(sku), new.get(sku)
        if a is not None and b is not None and all(a.get(k) == b.get(k) for k in DIFF_KEYS):
            continue
        status = "added" if a is None else "removed" if b is None else "changed"
        changes.append({"sku_id": sku, "sku_name": (b or a)["sku_name"], "change": status,
                        "before": _describe(a), "after": _describe(b),
                        "cash_delta": round((b or {}).get("cash_outlay", 0) - (a or {}).get("cash_outlay", 0), 2)})
    ps = prev.summary or {}
    return {
        "previous_plan_id": prev.id,
        "previous_as_of": prev.as_of.isoformat(),
        "previous_budget": prev.budget,
        "n_added": sum(1 for c in changes if c["change"] == "added"),
        "n_removed": sum(1 for c in changes if c["change"] == "removed"),
        "n_changed": sum(1 for c in changes if c["change"] == "changed"),
        "unchanged": len(set(old) & set(new)) - sum(1 for c in changes if c["change"] == "changed"),
        "delta_expected_loss": round(summary["expected_loss_plan"] - ps.get("expected_loss_plan", 0.0), 2),
        "delta_cash": round(summary["cash_used"] - ps.get("cash_used", 0.0), 2),
        "changes": changes,
    }


def create_plan(db: Session, budget: float | None = None, strategy: str = "commit_now") -> Plan:
    s = get_settings()
    budget = float(s.default_budget_inr if budget is None else budget)
    snap = state.load_snapshot(db)
    ctxs = state.build_contexts(snap)
    pw = state.planning_world(snap, ctxs)
    ew = state.evaluation_world(snap, ctxs)
    result, base, rows, summary = _plan_core(snap, ctxs, pw, ew, budget, strategy=strategy)
    prev = db.scalars(select(Plan).order_by(Plan.id.desc()).limit(1)).first()
    summary["diff"] = plan_diff(prev, rows, summary)
    summary["revision"] = 0
    method, exp, rec, err = expl.explain(get_llm(), explanation_input(summary, rows))
    if rec.status != "unavailable":
        persist_records(db, [rec])
    plan = Plan(as_of=snap.as_of, budget=budget, status="draft", actions=rows,
                baseline_actions=[a.to_dict() for a in base if a.kind != "defer"], summary=summary,
                explanation={"method": method, "error": err, **exp.model_dump()})
    db.add(plan)
    db.commit()
    queue_evaluation(db, plan)
    alerts_svc.upsert_alerts(db, alerts_svc.compute_alerts(ctxs, pw, snap.as_of))
    return plan


# ---------------------------------------------------------------- operator overrides

class PlanConflict(Exception):
    """The plan can no longer be edited (approved, or the data has moved on)."""


class InvalidOverride(ValueError):
    pass


MARKDOWN_RANGE = (0.05, 0.6)


def _kind(a: Action) -> str:
    if a.later_qty and a.order_qty and a.markdown is None:
        return "staggered_po"
    po = "" if not a.order_qty else ("expedited_po" if a.expedite else "standard_po")
    if a.markdown is not None:
        return "markdown" + (f"+{po}" if po else "")
    return po or "defer"


def apply_overrides(db: Session, plan: Plan, overrides: list[dict]) -> Plan:
    """Operator edits on a draft plan: change order qty, skip a SKU, or set a markdown. The edited plan is
    re-scored exactly like an optimised one (same planning and evaluation worlds)."""
    if plan.status != "draft":
        raise PlanConflict("only draft plans can be edited")
    snap = state.load_snapshot(db)
    if snap.as_of != plan.as_of:
        raise PlanConflict(f"plan was built for {plan.as_of}; data is now as of {snap.as_of}. Rebuild the plan.")
    ctxs = state.build_contexts(snap)
    by_ctx = {c.sku_id: c for c in ctxs}
    old_rows = {r["sku_id"]: r for r in plan.actions}
    acts = {c.sku_id: (Action.from_dict(old_rows[c.sku_id]["raw"], c.econ.unit_cost, c.expedite_premium_pct)
                       if c.sku_id in old_rows else defer(c)) for c in ctxs}
    touched = []
    for o in overrides:
        sku = o.get("sku_id")
        ctx = by_ctx.get(sku)
        if ctx is None:
            raise InvalidOverride(f"unknown sku {sku!r}")
        a = acts[sku]
        if o.get("skip"):
            a = defer(ctx)
        else:
            qty, md = a.order_qty, a.markdown
            expedite = bool(o["expedite"]) if o.get("expedite") is not None else a.expedite
            if o.get("order_qty") is not None:
                q = float(o["order_qty"])
                if q < 0:
                    raise InvalidOverride(f"{sku}: order quantity cannot be negative")
                qty = 0 if q == 0 else round_order_qty(q, ctx.moq, ctx.case_pack)
            if o.get("discount_pct") is not None:
                d = float(o["discount_pct"])
                if d == 0:
                    md = None
                else:
                    if not MARKDOWN_RANGE[0] - 1e-9 <= d <= MARKDOWN_RANGE[1] + 1e-9:
                        raise InvalidOverride(f"{sku}: discount must be between {MARKDOWN_RANGE[0]:.0%} and "
                                              f"{MARKDOWN_RANGE[1]:.0%}")
                    lots = sorted((l for l in ctx.lots if l.qty > 0), key=lambda l: l.expiry_day)
                    if md is None and not lots:
                        raise InvalidOverride(f"{sku}: no stock on hand to mark down")
                    md = Markdown(md.lot_id if md else lots[0].lot_id, round(d, 2), md.start_day if md else 0)
            a = Action(sku, "defer", ctx.econ.unit_cost, ctx.expedite_premium_pct, int(qty), bool(qty and expedite), md,
                       a.later_qty if qty and md is None else 0, a.later_day if qty and md is None else 0)
            a.kind = _kind(a)
        acts[sku] = a
        touched.append(sku)
    actions = [acts[c.sku_id] for c in ctxs]
    cash = sum(a.cash_outlay for a in actions)
    if cash > plan.budget + 1e-6:
        raise InvalidOverride(f"overrides need INR {cash:,.0f}, above the budget of INR {plan.budget:,.0f}")
    pw = state.planning_world(snap, ctxs)
    ew = state.evaluation_world(snap, ctxs)
    _, _, rows, summary = _plan_core(snap, ctxs, pw, ew, plan.budget, actions,
                                     strategy=(plan.summary or {}).get("strategy", "commit_now"))
    old_summary = plan.summary or {}
    removed_before = {x["sku_id"]: x["original"] for x in old_summary.get("overridden_removed", [])}
    for r in rows:
        prev = old_rows.get(r["sku_id"])
        if r["sku_id"] in touched or (prev and prev.get("overridden")) or r["sku_id"] in removed_before:
            r["overridden"] = True
            r["original"] = removed_before.get(r["sku_id"]) or (prev or {}).get("original") or \
                (_describe(prev) if prev else "No action")
    summary["diff"] = old_summary.get("diff")
    summary["revision"] = int(old_summary.get("revision", 0)) + 1
    log = list(old_summary.get("overrides", []))
    log.append({"at": utcnow().isoformat(), "items": overrides})
    summary["overrides"] = log
    skipped = [{"sku_id": k, "sku_name": v["sku_name"], "original": v.get("original") or _describe(v)}
               for k, v in old_rows.items() if k not in {r["sku_id"] for r in rows}]
    in_rows = {r["sku_id"] for r in rows} | {x["sku_id"] for x in skipped}
    summary["overridden_removed"] = skipped + [x for x in old_summary.get("overridden_removed", [])
                                               if x["sku_id"] not in in_rows]
    plan.actions = rows
    plan.summary = summary
    plan.explanation = {**(plan.explanation or {}),
                        "stale_note": "Plan edited by an operator after this explanation was written; "
                                      "the numbers above the explanation are current."}
    db.commit()
    queue_evaluation(db, plan)
    return plan


# ---------------------------------------------------------------- robustness (Monte Carlo, rolling, ablation)

def queue_evaluation(db: Session, plan: Plan) -> Job:
    job = enqueue(db, "evaluate_plan", {"plan_id": plan.id, "revision": (plan.summary or {}).get("revision", 0)})
    plan.summary = {**plan.summary, "robustness": {"status": "queued", "job_id": job.id}}
    db.commit()
    return job


def _plan_actions(plan: Plan, ctxs: list[SkuContext]) -> list[Action]:
    rows = {r["sku_id"]: r for r in plan.actions}
    return [Action.from_dict(rows[c.sku_id]["raw"], c.econ.unit_cost, c.expedite_premium_pct)
            if c.sku_id in rows else defer(c) for c in ctxs]


def _evaluate_options(plan: Plan, snap, ctxs: list[SkuContext], ew: World, worlds: list[World]) -> dict | None:
    """Plan options under weekly re-planning in the actual, calm and shock worlds (see rolling.compare_options)."""
    s = get_settings()
    rows = (plan.summary or {}).get("options") or []
    if not rows or s.option_worlds <= 0:
        return None
    by = {c.sku_id: c for c in ctxs}
    opts = {}
    for o in rows:
        acts = {d["sku_id"]: Action.from_dict(d, by[d["sku_id"]].econ.unit_cost, by[d["sku_id"]].expedite_premium_pct)
                for d in o["actions"] if d["sku_id"] in by}
        opts[o["key"]] = ([acts.get(c.sku_id) or defer(c) for c in ctxs], float(o["operating_budget"]))
    calm = worlds[:s.option_worlds]
    return rolling.compare_options(snap, ctxs, ew, calm, rolling.shock_worlds(calm, ctxs), opts,
                                   s.service_level_z, s.working_capital_rate)


def evaluate_plan(db: Session, plan_id: int, revision: int = 0) -> dict:
    s = get_settings()
    plan = db.get(Plan, plan_id)
    if plan is None:
        return {"skipped": "plan not found"}
    snap = state.load_snapshot(db, plan.as_of)
    ctxs = state.build_contexts(snap)
    pw = state.planning_world(snap, ctxs)
    ew = state.evaluation_world(snap, ctxs)
    acts = _plan_actions(plan, ctxs)
    policies = {"plan": acts, "static_baseline": baseline_actions(ctxs, plan.budget),
                "tuned_baseline": tuned_baseline(ctxs, pw, plan.budget)[0]}
    has_signals = any(e.source == "signal" for c in ctxs for e in state.demand_events(snap, c.sku_id)) or \
        any(state.delay_events(snap, c.sku_id) for c in ctxs)
    ablation = {"status": "no confirmed signals",
                "note": "No operator-confirmed signals are active, so the plan is identical with or without them."}
    if has_signals:
        snap2, ctxs2 = state.without_signals(snap, ctxs)
        nosig = build_plan(ctxs2, state.planning_world(snap2, ctxs2), plan.budget, s.service_level_z)
        policies["plan_without_signals"] = nosig.actions
        ev = value.compare(ctxs, acts, nosig.actions, ew)
        ablation = {"status": "done", "actual_world": {"with_signals": ev["plan"]["total_loss"],
                                                       "without_signals": ev["baseline"]["total_loss"],
                                                       "value_of_signals": ev["savings"]},
                    "n_changed_skus": sum(1 for a, b in zip(acts, nosig.actions) if a.key() != b.key())}
    worlds = state.monte_carlo_worlds(snap, ctxs, s.eval_worlds)
    mc = value.monte_carlo(ctxs, policies, worlds)
    if has_signals and "plan_vs_plan_without_signals" in mc["savings"]:
        ablation["monte_carlo"] = mc["savings"]["plan_vs_plan_without_signals"]
    roll = rolling.compare_rolling(snap, ctxs, [ew] + worlds[:s.rolling_worlds], acts, plan.budget,
                                   s.service_level_z)
    options = _evaluate_options(plan, snap, ctxs, ew, worlds)
    res = {"status": "done", "computed_at": utcnow().isoformat(), "revision": revision, "monte_carlo": mc,
           "rolling": roll, "options": options, "signal_ablation": ablation,
           "notes": ("Monte Carlo: demand = Poisson(actual x lognormal(0, 0.1) SKU shock); one historical delay "
                     "drawn per supplier per world. Decisions are fixed; only the world varies.")}
    db.refresh(plan)
    if int((plan.summary or {}).get("revision", 0)) == revision:
        plan.summary = {**plan.summary, "robustness": res}
        db.commit()
    return {"plan_id": plan_id, "revision": revision, "n_worlds": mc["n_worlds"]}


def approve_plan(db: Session, plan: Plan) -> Plan:
    st = get_storage()
    products = {p.sku_id: p for p in db.scalars(select(Product))}
    suppliers = {x.supplier_id: x for x in db.scalars(select(Supplier))}
    po_buf, md_buf = io.StringIO(), io.StringIO()
    po = csv.writer(po_buf)
    po.writerow(["draft_po_id", "supplier_id", "supplier_name", "sku_id", "sku_name", "qty", "unit_cost_inr",
                 "line_total_inr", "expedite", "expedite_premium_inr", "order_date", "status", "expected_arrival",
                 "plan_id"])
    md = csv.writer(md_buf)
    md.writerow(["sku_id", "sku_name", "lot_id", "discount_pct", "list_price_inr", "markdown_price_inr", "plan_id"])
    n_po = n_md = 0
    for r in plan.actions:
        p = products[r["sku_id"]]
        if r["order_qty"]:
            n_po += 1
            po.writerow([f"DPO-{plan.id}-{n_po:03d}", p.supplier_id, suppliers[p.supplier_id].name, p.sku_id, p.name,
                         r["order_qty"], p.unit_cost, round(r["order_qty"] * p.unit_cost, 2), r["expedite"],
                         r["expedite_premium"], plan.as_of.isoformat(), "place_now", r["arrival_date"], plan.id])
        if r.get("later_qty"):
            n_po += 1
            po.writerow([f"DPO-{plan.id}-{n_po:03d}", p.supplier_id, suppliers[p.supplier_id].name, p.sku_id, p.name,
                         r["later_qty"], p.unit_cost, round(r["later_qty"] * p.unit_cost, 2), False, 0.0,
                         r["later_order_date"], "scheduled_reconfirm_at_review", r["later_arrival_date"], plan.id])
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
BACKTEST_CUTOFFS = (42, 28, 14)
BACKTEST_SKUS = 10


def _backtest_skus(snap) -> tuple[list[str], list[str], list[str]]:
    """Top-volume SKUs plus every SKU with a scoped context event (numeric prior or text-only) overlapping a hold-out
    window. Returns (selected, top_volume, text_only_skus)."""
    top = sorted(snap.products, key=lambda k: -float(snap.history[k][-56:].sum()))[:BACKTEST_SKUS]
    windows = [(snap.as_of - timedelta(days=c), snap.as_of - timedelta(days=c - BACKTEST_DAYS + 1))
               for c in BACKTEST_CUTOFFS]
    with_events, text_only = [], []
    for sku in snap.products:
        hits = [e for e in state.demand_events(snap, sku, include_signals=False)
                if any(e.start <= hi and e.end >= lo for lo, hi in windows)]
        if hits:
            with_events.append(sku)
        if any(not e.prior_known for e in hits):
            text_only.append(sku)
    return top + [s for s in with_events if s not in top], top, text_only


def _acc() -> dict:
    return {"a": [], "f": [], "lo": [], "hi": []}


def _add(acc: dict, actual, f, lo, hi) -> None:
    acc["a"].extend(actual)
    acc["f"].extend(f)
    acc["lo"].extend(lo)
    acc["hi"].extend(hi)


def _score(acc: dict) -> dict:
    return {"wape": _w(acc["a"], acc["f"]), "coverage_p10_p90": _cov(acc), "n_points": len(acc["a"])}


def run_backtest(db: Session) -> dict:
    """Rolling-origin backtest: forecast 14 days from three past origins (42, 28 and 14 days before as-of) and score
    against what actually sold, on the top-volume SKUs plus every SKU with a context event in a hold-out window.
    Models: seasonal naive, seasonal naive with the feed's numeric priors, the statistical base (Holt-Winters /
    Croston + numeric priors; text-only events count as x1.0) and the hybrid (model-tuned priors plus estimates for
    text-only events, all origins; responses cached by input hash). Confirmed operator signals are excluded."""
    from ..llm.cache import cached

    snap = state.load_snapshot(db)
    llm = cached(get_llm())
    skus, top, text_skus = _backtest_skus(snap)
    names = ("naive", "naive_events", "statistical")
    pooled = {m: _acc() for m in (*names, "hybrid")}
    stat_same, text_stat, text_hyb = _acc(), _acc(), _acc()
    per_origin, per_sku, hybrid_jobs = [], {}, []
    o_accs = {}
    for cut in BACKTEST_CUTOFFS:
        as_of = snap.as_of - timedelta(days=cut)
        last = as_of + timedelta(days=BACKTEST_DAYS - 1)
        o_acc = o_accs[as_of.isoformat()] = {m: _acc() for m in (*names, "hybrid")}
        for sku in skus:
            hist = snap.history[sku][:-cut]
            actual = snap.history[sku][len(hist):len(hist) + BACKTEST_DAYS]
            evs = state.demand_events(snap, sku, include_signals=False)
            text_hit = any(not e.prior_known and e.start <= last and e.end >= as_of for e in evs)
            n10, n50, n90 = fc.seasonal_naive(hist, BACKTEST_DAYS)
            m, _ = fc.multipliers(as_of, BACKTEST_DAYS, [e for e in evs if e.end >= as_of])
            p10, p50, p90, _, base = fc.statistical_forecast(hist, as_of, BACKTEST_DAYS, evs)
            for name, f, lo, hi in (("naive", n50, n10, n90), ("naive_events", n50 * m, n10 * m, n90 * m),
                                    ("statistical", p50, p10, p90)):
                _add(o_acc[name], actual, f, lo, hi)
                _add(pooled[name], actual, f, lo, hi)
            row = per_sku.setdefault(sku, {"sku_id": sku, "selected_by": "volume" if sku in top else "context event",
                                           "text_only_event": sku in text_skus, "hybrid_methods": []})
            if cut == BACKTEST_CUTOFFS[-1]:
                row.update({"wape_naive": _w(actual, n50), "wape_statistical": _w(actual, p50),
                            "hybrid_method": None, "wape_hybrid": None})
            if llm is not None:
                p = snap.products[sku]
                rows = [r for r in state.context_rows(snap, sku) if r["start"] <= last.isoformat()]
                summary = fc.build_summary({"sku_id": sku, "name": p.name, "category": p.category}, hist, as_of,
                                           BACKTEST_DAYS, evs, rows, base.base_p50)
                hybrid_jobs.append({"sku_id": sku, "summary": summary, "history": hist, "as_of": as_of,
                                    "horizon": BACKTEST_DAYS, "events": evs, "actual": actual, "cut": cut,
                                    "stat": (p50, p10, p90), "text_hit": text_hit,
                                    "hash": fc.forecast_hash(summary, llm.model)})
    hybrid_ok = 0
    if hybrid_jobs:
        outs = fc.forecast_many(llm, hybrid_jobs, get_settings().llm_max_concurrency)
        for j, o in zip(hybrid_jobs, outs):
            persist_records(db, [r for r in o.records if r.status != "unavailable"])
            row = per_sku[j["sku_id"]]
            row["hybrid_methods"].append(o.method)
            if j["cut"] == BACKTEST_CUTOFFS[-1]:
                row["hybrid_method"] = o.method
            if o.method != "hybrid":
                continue
            hybrid_ok += 1
            if j["cut"] == BACKTEST_CUTOFFS[-1]:
                row["wape_hybrid"] = _w(j["actual"], o.p50)
            _add(pooled["hybrid"], j["actual"], o.p50, o.p10, o.p90)
            _add(o_accs[j["as_of"].isoformat()]["hybrid"], j["actual"], o.p50, o.p10, o.p90)
            _add(stat_same, j["actual"], *j["stat"])
            if j["text_hit"]:
                _add(text_stat, j["actual"], *j["stat"])
                _add(text_hyb, j["actual"], o.p50, o.p10, o.p90)
    for origin, o_acc in o_accs.items():
        per_origin.append({"origin": origin, **{f"wape_{k}": _w(v["a"], v["f"]) for k, v in o_acc.items() if v["a"]},
                           **{f"coverage_{k}": _cov(v) for k, v in o_acc.items() if v["a"]}})
    latest = BACKTEST_CUTOFFS[-1]
    result = {
        "origins": [o["origin"] for o in per_origin],
        "holdout_start": (snap.as_of - timedelta(days=latest)).isoformat(),
        "holdout_days": BACKTEST_DAYS,
        "skus": len(skus),
        "sku_selection": {"top_volume": top, "context_event": [s for s in skus if s not in top],
                          "text_only_event": text_skus},
        "pooled": {k: _score(v) for k, v in pooled.items() if v["a"]},
        "per_origin": per_origin,
        "hybrid_attempted": len(hybrid_jobs),
        "hybrid_valid_forecasts": hybrid_ok,
        "statistical_on_hybrid_points": _score(stat_same) if stat_same["a"] else None,
        "wape_statistical_on_hybrid_skus": _w(stat_same["a"], stat_same["f"]) if stat_same["a"] else None,
        "text_only_subset": ({"n_series": len(text_hyb["a"]) // BACKTEST_DAYS, "statistical": _score(text_stat),
                              "hybrid": _score(text_hyb)} if text_hyb["a"] else None),
        "per_sku": list(per_sku.values()),
        "computed_at": utcnow().isoformat(),
        "note": (f"WAPE = sum|actual - forecast| / sum actual (lower is better); coverage = share of actual days "
                 f"inside the p10-p90 band (target 0.8). SKUs: top {BACKTEST_SKUS} by volume plus every SKU with a "
                 "context event (numeric prior or text-only) in a hold-out window. Text-only events count as x1.0 "
                 "for naive+events and statistical; the hybrid estimates them from their description. Hybrid runs "
                 "on all origins; 'statistical_on_hybrid_points' scores the statistical model on exactly the "
                 "series where the hybrid output was valid; 'text_only_subset' restricts that to series whose "
                 "hold-out overlaps a text-only event."),
    }
    row = db.get(AppState, "backtest")
    if row is None:
        db.add(AppState(key="backtest", value=result))
    else:
        row.value = result
    db.commit()
    return result


def _w(a, f) -> float | None:
    v = value.wape(a, f)
    return None if v is None else round(v, 4)


def _cov(acc: dict) -> float | None:
    v = fc.coverage(acc["a"], acc["lo"], acc["hi"]) if acc["a"] else None
    return None if v is None else round(v, 4)


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
            elif job.kind == "evaluate_plan":
                res = evaluate_plan(db, int(job.payload["plan_id"]), int(job.payload.get("revision", 0)))
            elif job.kind == "extraction_eval":
                from .extraction_eval import run_extraction_eval
                res = run_extraction_eval(db)
            else:
                raise ValueError(f"unknown job kind {job.kind}")
            job.result, job.status = res, "done"
        except Exception as exc:  # recorded on the job row
            db.rollback()
            job = db.get(Job, job_id)
            job.status, job.error = "failed", f"{type(exc).__name__}: {exc}"[:2000]
            if job.kind == "evaluate_plan":
                plan = db.get(Plan, int(job.payload.get("plan_id", 0)))
                if plan is not None and (plan.summary or {}).get("robustness", {}).get("job_id") == job.id:
                    plan.summary = {**plan.summary, "robustness": {"status": "failed", "job_id": job.id,
                                                                   "error": job.error}}
        job.finished_at = utcnow()
        db.commit()
    finally:
        db.close()


def recompute_alerts(db: Session) -> dict:
    snap = state.load_snapshot(db)
    ctxs = state.build_contexts(snap)
    return alerts_svc.upsert_alerts(db, alerts_svc.compute_alerts(ctxs, state.planning_world(snap, ctxs), snap.as_of))

"""Read-only tools for the planning assistant. Each one wraps the deterministic code (state, planner, world, value,
scenarios) and returns compact JSON. Nothing is written to the database: previews, alternatives and scenarios are
computed in memory."""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import date, timedelta
from functools import cached_property
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..llm.agent import ToolSpec
from ..models import Alert, Plan, SignalEvent
from . import plans as plan_svc
from . import scenarios as scen
from . import state, value
from .planner import Menu, build_plan, prepare_menu, solve_menu
from .simulator import Markdown
from .supply import round_order_qty
from .world import Action, Outcome, SkuContext, World, defer, evaluate

MAX_ROWS = 12
MAX_BUDGET = 10_000_000.0
MARKDOWN_RANGE = getattr(plan_svc, "MARKDOWN_RANGE", (0.05, 0.6))
FRONTIER_MULTIPLES = getattr(plan_svc, "FRONTIER_MULTIPLES", (0.0, 0.25, 0.5, 0.75, 1.0, 1.5, 2.0))
KIND_LABEL = getattr(plan_svc, "KIND_LABEL", {})


class ToolError(ValueError):
    """Bad tool arguments; the message is returned to the model so it can correct the call."""


def _r(x: float | None, n: int = 2) -> float | None:
    return None if x is None else round(float(x), n)


def _kind(a: Action) -> str:
    po = "" if not a.order_qty else ("expedited_po" if a.expedite else "standard_po")
    if a.markdown is not None:
        return "markdown" + (f"+{po}" if po else "")
    return po or "defer"


def _describe(a: Action) -> str:
    if a.kind == "defer":
        return "No action (defer)"
    parts = [KIND_LABEL.get(a.kind, a.kind)]
    if a.order_qty:
        parts.append(f"{a.order_qty} units")
    if a.markdown is not None:
        parts.append(f"{round(a.markdown.discount_pct * 100)}% off lot {a.markdown.lot_id} from day {a.markdown.start_day}")
    return ", ".join(parts)


def _budgets_for(budget: float) -> list[float]:
    return sorted({budget * m for m in FRONTIER_MULTIPLES} | {budget})


def _bounded_budget(v: Any, name: str = "budget") -> float:
    try:
        b = float(v)
    except (TypeError, ValueError):
        raise ToolError(f"{name} must be a number in INR")
    if not 0 <= b <= MAX_BUDGET:
        raise ToolError(f"{name} must be between 0 and {MAX_BUDGET:,.0f} INR")
    return round(b, 2)


class ToolContext:
    """Per-request cache of the snapshot, contexts, worlds and the latest plan."""

    def __init__(self, db: Session):
        self.db = db
        self.z = get_settings().service_level_z

    @cached_property
    def snap(self) -> state.Snapshot:
        return state.load_snapshot(self.db)

    @cached_property
    def ctxs(self) -> list[SkuContext]:
        return state.build_contexts(self.snap)

    @cached_property
    def by_id(self) -> dict[str, SkuContext]:
        return {c.sku_id: c for c in self.ctxs}

    @cached_property
    def pw(self) -> World:
        return state.planning_world(self.snap, self.ctxs)

    @cached_property
    def ew(self) -> World:
        return state.evaluation_world(self.snap, self.ctxs)

    @cached_property
    def plan(self) -> Plan | None:
        return self.db.scalars(select(Plan).order_by(Plan.id.desc()).limit(1)).first()

    @cached_property
    def plan_rows(self) -> dict[str, dict]:
        return {r["sku_id"]: r for r in (self.plan.actions if self.plan else []) if r.get("sku_id")}

    @cached_property
    def plan_actions(self) -> dict[str, Action]:
        """The latest plan's decisions (operator overrides included); defer for SKUs without an action."""
        if self.plan is None:
            return {c.sku_id: defer(c) for c in self.ctxs}
        try:
            acts = plan_svc._plan_actions(self.plan, self.ctxs)
        except Exception:  # plan rows from another schema version: fall back to the stored raw actions
            acts = [Action.from_dict(self.plan_rows[c.sku_id]["raw"], c.econ.unit_cost, c.expedite_premium_pct)
                    if c.sku_id in self.plan_rows and self.plan_rows[c.sku_id].get("raw") else defer(c)
                    for c in self.ctxs]
        return {a.sku_id: a for a in acts}

    @property
    def budget(self) -> float:
        return float(self.plan.budget) if self.plan else float(get_settings().default_budget_inr)

    def day(self, d: int | None) -> str | None:
        return None if d is None else (self.snap.as_of + timedelta(days=int(d))).isoformat()

    def sku(self, sku_id: Any) -> SkuContext:
        s = str(sku_id or "").strip().upper()
        m = re.fullmatch(r"(?:SKU)?[- ]?0*(\d{1,3})", s)
        if m:
            s = f"SKU-{int(m.group(1)):03d}"
        if s not in self.by_id:
            raise ToolError(f"unknown SKU {sku_id!r}; ids look like SKU-001..SKU-{len(self.by_id):03d} "
                            "(use list_skus to look up names)")
        return self.by_id[s]

    def supplier(self, supplier_id: Any) -> str:
        s = str(supplier_id or "").strip().upper()
        m = re.fullmatch(r"(?:SUP)?[- ]?0*(\d{1,2})", s)
        if m:
            s = f"SUP-{int(m.group(1)):02d}"
        if s in self.snap.suppliers:
            return s
        for sid, sup in self.snap.suppliers.items():
            if s and s.lower() in sup.name.lower():
                return sid
        raise ToolError(f"unknown supplier {supplier_id!r}; known: {sorted(self.snap.suppliers)}")

    def plan_meta(self) -> dict:
        p = self.plan
        if p is None:
            return {"plan_id": None, "note": "No plan has been built yet (create one on the Recovery Plan page). "
                    "'Current plan' / 'plan action' in these results means doing nothing: every SKU deferred, "
                    "INR 0 spent."}
        out = {"plan_id": p.id, "plan_status": p.status, "plan_as_of": p.as_of.isoformat(), "budget": p.budget}
        if p.as_of != self.snap.as_of:
            out["stale"] = f"plan built for {p.as_of}; data is now as of {self.snap.as_of}"
        return out


# ---------------------------------------------------------------- formatting helpers

def _plan_outcome(o: Outcome) -> dict:
    fr = o.fill_rate
    return {"expected_loss": _r(o.total_loss), "service_penalty": _r(o.service_penalty),
            "objective": _r(o.objective), "units_short": _r(o.units_short, 1),
            "expired_units": _r(o.expired_units, 1), "markdown_units": _r(o.markdown_units, 1),
            "fill_rate": None if fr is None else round(fr, 4)}


def _eval_outcome(o: Outcome) -> dict:
    fr = o.fill_rate
    return {"total_loss": _r(o.total_loss), "units_short": _r(o.units_short, 1),
            "expired_units": _r(o.expired_units, 1), "fill_rate": None if fr is None else round(fr, 4)}


def _action_brief(a: Action, tc: ToolContext, sw=None) -> dict:
    d = {"sku_id": a.sku_id, "action": _describe(a), "kind": a.kind, "order_qty": a.order_qty,
         "expedite": a.expedite, "cash": _r(a.cash_outlay)}
    if a.markdown is not None:
        d.update(markdown_lot_id=a.markdown.lot_id, discount_pct=a.markdown.discount_pct)
    if sw is not None and a.order_qty:
        d["arrival_date"] = tc.day(sw.expedite_arrival_day if a.expedite else sw.standard_arrival_day)
    return d


def _totals_brief(t: dict) -> dict:
    return {k: t.get(k) for k in ("total_loss", "fill_rate", "units_short", "expired_units", "cash_outlay")}


def _eval_policy(tc: ToolContext, ctxs: list[SkuContext], acts: dict[str, Action], world: World) -> dict:
    ev = value.evaluate_actions(ctxs, [acts[c.sku_id] for c in ctxs], world)
    return {**_totals_brief(ev["totals"]), "hero_fill_rate": value.hero_fill_rate(ctxs, ev["per_sku"])}


def _planning_totals(ctxs: list[SkuContext], acts: dict[str, Action], world: World) -> dict:
    loss = pen = 0.0
    for c in ctxs:
        o = evaluate(c, acts[c.sku_id], world.skus[c.sku_id])
        loss += o.total_loss
        pen += o.service_penalty
    return {"expected_loss": round(loss, 2), "service_penalty": round(pen, 2), "objective": round(loss + pen, 2),
            "cash_used": round(sum(a.cash_outlay for a in acts.values()), 2)}


def _diff(tc: ToolContext, before: dict[str, Action], after: dict[str, Action]) -> list[dict]:
    out = []
    for c in tc.ctxs:
        a, b = before.get(c.sku_id) or defer(c), after.get(c.sku_id) or defer(c)
        if a.key() == b.key():
            continue
        out.append({"sku_id": c.sku_id, "sku_name": c.name, "priority": c.priority, "before": _describe(a),
                    "after": _describe(b), "cash_before": _r(a.cash_outlay), "cash_after": _r(b.cash_outlay),
                    "cash_change": _r(b.cash_outlay - a.cash_outlay)})
    return out


def _delta(new: dict, old: dict, keys: tuple[str, ...], n: int = 2) -> dict:
    return {f"{k}_change": (None if new.get(k) is None or old.get(k) is None else round(new[k] - old[k], n))
            for k in keys}


# ---------------------------------------------------------------- tools

def get_plan_summary(tc: ToolContext) -> dict:
    p = tc.plan
    if p is None:
        return tc.plan_meta()
    s = p.summary or {}
    card = s.get("scorecard") or {}
    svc = s.get("service") or {}
    rob = s.get("robustness") or {}
    mc = (rob.get("monte_carlo") or {}).get("savings") or {}
    nol, pl = s.get("expected_loss_no_action"), s.get("expected_loss_plan")
    rows = sorted(p.actions or [], key=lambda r: -(r.get("loss_avoided") or 0))
    return {
        **tc.plan_meta(),
        "horizon_days": get_settings().horizon_days,
        "cash_used": s.get("cash_used"), "cash_remaining": s.get("cash_remaining"),
        "planning_world": {"expected_loss_no_action": nol, "expected_loss_with_plan": pl,
                           "expected_loss_avoided": _r(nol - pl) if nol is not None and pl is not None else None,
                           "skus_at_risk": s.get("skus_at_risk")},
        "evaluation_world": {
            "plan_total_loss": (card.get("plan") or {}).get("total_loss"),
            "static_baseline_total_loss": (card.get("baseline") or {}).get("total_loss"),
            "savings_vs_static_baseline": card.get("savings"),
            "tuned_baseline_total_loss": (card.get("tuned_baseline") or {}).get("total_loss"),
            "savings_vs_tuned_baseline": card.get("savings_vs_tuned"),
            "plan_fill_rate": svc.get("plan_fill_rate"), "static_baseline_fill_rate": svc.get("baseline_fill_rate"),
            "tuned_baseline_fill_rate": svc.get("tuned_fill_rate"),
            "plan_hero_fill_rate": svc.get("plan_hero_fill_rate"),
            "static_baseline_hero_fill_rate": svc.get("baseline_hero_fill_rate"),
        },
        "hero_skus": svc.get("hero_skus"),
        "robustness": {"status": rob.get("status"), "n_worlds": (rob.get("monte_carlo") or {}).get("n_worlds"),
                       **{k: {m: v.get(m) for m in ("mean", "p5", "p95", "win_rate")} for k, v in mc.items()}},
        "n_actions": len(rows),
        "actions": [{k: r.get(k) for k in ("sku_id", "sku_name", "kind", "order_qty", "expedite", "arrival_date",
                                           "markdown_lot_id", "discount_pct", "cash_outlay", "loss_avoided")}
                    | {"reasons": (r.get("reasons") or [])[:2], **({"overridden": True} if r.get("overridden") else {})}
                    for r in rows[:MAX_ROWS]],
    }


def list_skus(tc: ToolContext, name_contains: str | None = None, category: str | None = None,
              supplier_id: str | None = None, priority: str | None = None) -> dict:
    sid = tc.supplier(supplier_id) if supplier_id else None
    out = []
    for c in tc.ctxs:
        if name_contains and str(name_contains).lower() not in c.name.lower():
            continue
        if category and str(category).lower() not in c.category.lower():
            continue
        if sid and c.supplier_id != sid:
            continue
        if priority and c.priority != str(priority).lower():
            continue
        a = tc.plan_actions[c.sku_id]
        lots = sorted(c.lots, key=lambda l: l.expiry_day)
        out.append({"sku_id": c.sku_id, "name": c.name, "category": c.category, "supplier_id": c.supplier_id,
                    "priority": c.priority, "on_hand": round(c.on_hand), "open_po_qty": round(sum(p.qty for p in c.open_pos)),
                    "earliest_expiry": tc.day(lots[0].expiry_day) if lots else None, "plan_action": _describe(a)})
    return {"count": len(out), "skus": out[:30]}


def _compare_alt(in_plan: bool, extra: float, dobj: float, cash_left: float | None, has_plan: bool = True) -> str:
    """Plain-language reading of an alternative vs the plan's action (signs are easy to misread)."""
    if in_plan:
        return "this is the plan's action" if has_plan else "doing nothing (no plan has been built yet)"
    cash = (f"needs INR {extra:,.0f} more cash" if extra > 0.5 else
            f"frees INR {-extra:,.0f} of cash" if extra < -0.5 else "same cash")
    if dobj < -0.5:
        better = f"lowers this SKU's expected loss + service penalty by INR {-dobj:,.0f}"
        if extra > 0.5:
            if not has_plan:
                return f"{better} vs doing nothing and {cash}; no plan has been built yet to choose between options"
            fit = ("which does not fit the remaining budget" if cash_left is not None and extra > cash_left + 1e-6
                   else "which the optimiser judged better spent on other SKUs")
            return f"{better} but {cash}, {fit}"
        return f"{better} and {cash}"
    if dobj > 0.5:
        return f"worse for this SKU: raises expected loss + service penalty by INR {dobj:,.0f}; {cash}"
    return f"about the same outcome for this SKU; {cash}"


def get_sku(tc: ToolContext, sku_id: str) -> dict:
    c = tc.sku(sku_id)
    snap = tc.snap
    sw = tc.pw.skus[c.sku_id]
    p = snap.products[c.sku_id]
    sup = snap.suppliers[c.supplier_id]
    m = c.metrics
    pri = state.sku_priorities(snap)[c.sku_id]
    menu = prepare_menu([c], tc.pw, _budgets_for(tc.budget), tc.z).skus[0]
    rm = menu.reorder
    plan_a = tc.plan_actions[c.sku_id]
    plan_o = evaluate(c, plan_a, sw)
    d_o = menu.defer_outcome
    row = tc.plan_rows.get(c.sku_id) or {}
    cash_left = (tc.plan.summary or {}).get("cash_remaining") if tc.plan else None

    alts = []
    for a, o in zip(menu.actions, menu.outcomes):
        alts.append((a, o))
    if all(a.key() != plan_a.key() for a, _ in alts):
        alts.append((plan_a, plan_o))
    alts.sort(key=lambda x: x[1].objective)
    picked: list[tuple[Action, Outcome]] = []
    seen_kinds: set[str] = set()
    for a, o in alts:  # plan action, best per kind, then the overall best few
        if a.key() == plan_a.key() or a.kind not in seen_kinds:
            picked.append((a, o))
            seen_kinds.add(a.kind)
    for a, o in alts[:3]:
        if all(a.key() != x.key() for x, _ in picked):
            picked.append((a, o))
    picked.sort(key=lambda x: x[1].objective)
    alternatives = []
    for a, o in picked[:8]:
        extra = a.cash_outlay - plan_a.cash_outlay
        dobj = o.objective - plan_o.objective
        alternatives.append({
            **_action_brief(a, tc, sw), "in_plan": a.key() == plan_a.key(),
            "expected_loss": _r(o.total_loss), "service_penalty": _r(o.service_penalty), "objective": _r(o.objective),
            "loss_avoided_vs_no_action": _r(d_o.total_loss - o.total_loss),
            "extra_cash_vs_plan": _r(extra), "objective_change_vs_plan": _r(dobj),
            "fits_remaining_budget": None if cash_left is None else extra <= cash_left + 1e-6,
            "comparison": _compare_alt(a.key() == plan_a.key(), extra, dobj, cash_left, tc.plan is not None),
        })
    f = c.forecast
    return {
        **tc.plan_meta(),
        "sku_id": c.sku_id, "name": c.name, "category": c.category, "as_of": snap.as_of.isoformat(),
        "unit_cost": p.unit_cost, "unit_price": p.unit_price, "moq": c.moq, "case_pack": c.case_pack,
        "shelf_life_days": p.shelf_life_days,
        "priority": {"priority": pri["priority"], "automatic": pri["auto"], "overridden": pri["overridden"],
                     "service_penalty_per_unit_short": _r(c.goodwill_per_unit)},
        "lots": [{"lot_id": l.lot_id, "qty": l.qty, "expiry_date": tc.day(l.expiry_day), "days_to_expiry": l.expiry_day}
                 for l in sorted(c.lots, key=lambda l: l.expiry_day)][:MAX_ROWS],
        "open_pos": [{"po_id": o.po_id, "qty": o.qty, "expected_date": tc.day(o.expected_day),
                      "planning_arrival_date": tc.day(next((i.arrival_day for i in sw.open_inbound if i.ref_id == o.po_id),
                                                           None))} for o in c.open_pos][:MAX_ROWS],
        "forecast_30d": {"method": f.method, "p10_total": round(sum(f.p10), 1), "p50_total": round(sum(f.p50), 1),
                         "p90_total": round(sum(f.p90), 1), "p50_daily_mean": round(sum(f.p50) / max(1, len(f.p50)), 2),
                         "drivers": list(f.drivers or [])[:4]},
        "supplier": {"supplier_id": c.supplier_id, "name": sup.name, "stated_lead_time_days": c.stated_lead_time,
                     "on_time_rate": m.on_time_rate, "p80_delay_days": m.p80_delay_days,
                     "mean_delay_days": m.mean_delay_days, "expedite_lead_time_days": c.expedite_lead_time,
                     "expedite_premium_pct": c.expedite_premium_pct,
                     "new_standard_po_arrival_date": tc.day(sw.standard_arrival_day),
                     "new_expedited_po_arrival_date": tc.day(sw.expedite_arrival_day),
                     "supply_cases": [{"label": s.label, "weight": s.weight,
                                       "new_po_arrival_date": tc.day(s.standard_arrival_day)} for s in sw.supply_cases]},
        "reorder_math": {"mean_daily_demand": _r(rm.mean_daily_demand), "safety_stock": _r(rm.safety_stock),
                         "reorder_point": _r(rm.reorder_point), "inventory_position": _r(rm.position),
                         "lead_time_days": rm.lead_time_days, "z": rm.z},
        "no_action_outcome": {**_plan_outcome(d_o), "first_stockout_date": tc.day(d_o.first_stockout_day),
                              "expired_by_lot": {k: round(v, 1) for k, v in d_o.expired_by_lot.items() if v > 0.5}},
        "plan_action": {**_action_brief(plan_a, tc, sw), **_plan_outcome(plan_o),
                        "loss_avoided": _r(d_o.total_loss - plan_o.total_loss),
                        "reasons": (row.get("reasons") or [])[:4], "overridden": bool(row.get("overridden"))},
        "plan_cash_remaining": cash_left,
        "alternatives_considered": alternatives,
        "n_candidates_in_menu": len(menu.actions),
        "notes": list(sw.notes)[:4],
        "how_to_read": "Planning-world numbers (expected over forecast bands and supplier delay cases). The optimiser "
                       "minimises objective = expected loss + service penalty across all SKUs under the budget, so an "
                       "alternative with a lower objective may still lose out if its extra cash does more good elsewhere.",
    }


def evaluate_alternative(tc: ToolContext, sku_id: str, order_qty: float | None = None, expedite: bool | None = None,
                         discount_pct: float | None = None, markdown_lot_id: str | None = None,
                         markdown_start_day: int | None = None, skip: bool | None = None) -> dict:
    c = tc.sku(sku_id)
    sw, ew_sw = tc.pw.skus[c.sku_id], tc.ew.skus[c.sku_id]
    plan_a = tc.plan_actions[c.sku_id]
    notes: list[str] = []
    if skip:
        alt = defer(c)
    else:
        qty, md, exp = plan_a.order_qty, plan_a.markdown, plan_a.expedite
        if expedite is not None:
            exp = bool(expedite)
        if order_qty is not None:
            try:
                q = float(order_qty)
            except (TypeError, ValueError):
                raise ToolError("order_qty must be a number of units")
            if not 0 <= q <= 100_000:
                raise ToolError("order_qty must be between 0 and 100000 units")
            qty = 0 if q == 0 else round_order_qty(q, c.moq, c.case_pack)
            if qty != q:
                notes.append(f"order quantity {q:g} rounded to {qty} (MOQ {c.moq}, case pack {c.case_pack})")
        if exp and not qty:
            raise ToolError("expedite needs an order quantity: pass order_qty (the plan has no order for this SKU)")
        if exp and sw.expedite_arrival_day is None:
            raise ToolError(f"supplier {c.supplier_id} has no expedite option")
        if discount_pct is not None:
            try:
                d = float(discount_pct)
            except (TypeError, ValueError):
                raise ToolError("discount_pct must be a fraction such as 0.35")
            if d > 1:
                d = d / 100
            if d == 0:
                md = None
            else:
                if not MARKDOWN_RANGE[0] - 1e-9 <= d <= MARKDOWN_RANGE[1] + 1e-9:
                    raise ToolError(f"discount must be between {MARKDOWN_RANGE[0]:.0%} and {MARKDOWN_RANGE[1]:.0%}")
                lots = sorted((l for l in c.lots if l.qty > 0), key=lambda l: l.expiry_day)
                if not lots:
                    raise ToolError(f"{c.sku_id} has no stock on hand to mark down")
                lot_id = md.lot_id if md else lots[0].lot_id
                if markdown_lot_id:
                    lot_id = str(markdown_lot_id).strip().upper()
                    if lot_id not in {l.lot_id for l in lots}:
                        raise ToolError(f"unknown lot {markdown_lot_id!r}; lots: {[l.lot_id for l in lots]}")
                start = md.start_day if md else 0
                if markdown_start_day is not None:
                    start = int(markdown_start_day)
                    if not 0 <= start <= 29:
                        raise ToolError("markdown_start_day must be between 0 and 29")
                md = Markdown(lot_id, round(d, 2), start)
        alt = Action(c.sku_id, "defer", c.econ.unit_cost, c.expedite_premium_pct, int(qty), bool(qty and exp), md)
        alt.kind = _kind(alt)
    po, ao, do = evaluate(c, plan_a, sw), evaluate(c, alt, sw), evaluate(c, defer(c), sw)
    peo, aeo = evaluate(c, plan_a, ew_sw), evaluate(c, alt, ew_sw)
    plan_cash = sum(a.cash_outlay for a in tc.plan_actions.values())
    with_alt = plan_cash - plan_a.cash_outlay + alt.cash_outlay
    d_obj = ao.objective - po.objective
    verdict = ("same as the plan" if alt.key() == plan_a.key() else
               f"alternative is {'better' if d_obj < 0 else 'worse'} than the plan's action in the planning world "
               f"(objective change {d_obj:+,.2f} INR)")
    return {
        **tc.plan_meta(),
        "sku_id": c.sku_id, "name": c.name, "priority": c.priority,
        "plan_action": _action_brief(plan_a, tc, sw), "alternative": _action_brief(alt, tc, sw),
        "planning_world": {"plan": _plan_outcome(po), "alternative": _plan_outcome(ao), "no_action": _plan_outcome(do),
                           "expected_loss_change": _r(ao.total_loss - po.total_loss),
                           "objective_change": _r(d_obj),
                           "units_short_change": _r(ao.units_short - po.units_short, 1),
                           "expired_units_change": _r(ao.expired_units - po.expired_units, 1)},
        "evaluation_world": {"plan": _eval_outcome(peo), "alternative": _eval_outcome(aeo),
                             "total_loss_change": _r(aeo.total_loss - peo.total_loss)},
        "cash": {"plan_action_cash": _r(plan_a.cash_outlay), "alternative_cash": _r(alt.cash_outlay),
                 "cash_change": _r(alt.cash_outlay - plan_a.cash_outlay), "budget": tc.budget,
                 "plan_cash_used": _r(plan_cash), "plan_cash_used_with_alternative": _r(with_alt),
                 "fits_budget": with_alt <= tc.budget + 1e-6},
        "verdict": verdict, "notes": notes,
        "how_to_read": "Changes are alternative minus plan (negative loss change = alternative loses less). "
                       "Evaluation world = replay against simulated actual demand and lead times. Nothing is saved.",
    }


def replan_preview(tc: ToolContext, budget: float | None = None, hero_skus: list[str] | None = None,
                   standard_skus: list[str] | None = None, protect_skus: list[str] | None = None,
                   protect_hero: bool | None = None) -> dict:
    bud = _bounded_budget(budget) if budget is not None else tc.budget
    over = dict(tc.snap.priority_overrides or {})
    heroes = [tc.sku(s).sku_id for s in (hero_skus or [])][:30]
    stds = [tc.sku(s).sku_id for s in (standard_skus or [])][:30]
    if set(heroes) & set(stds):
        raise ToolError("a SKU cannot be both hero and standard")
    over.update({s: "hero" for s in heroes})
    over.update({s: "standard" for s in stds})
    snap2 = replace(tc.snap, priority_overrides=over)
    ctxs2 = state.build_contexts(snap2)
    pw2, ew2 = state.planning_world(snap2, ctxs2), state.evaluation_world(snap2, ctxs2)
    current = tc.plan_actions
    protected = {tc.sku(s).sku_id for s in (protect_skus or [])}
    if protect_hero:
        protected |= {c.sku_id for c in ctxs2 if c.priority == "hero"}
    protected = {s for s in protected if current[s].kind != "defer"}
    p_cash = sum(current[s].cash_outlay for s in protected)
    if p_cash > bud + 1e-6:
        raise ToolError(f"protected actions need INR {p_cash:,.2f}, more than the budget of INR {bud:,.2f}")
    menu = prepare_menu(ctxs2, pw2, _budgets_for(bud), tc.z)
    free = Menu([m for m in menu.skus if m.ctx.sku_id not in protected])
    res = solve_menu(free, bud - p_cash)
    new = {d.ctx.sku_id: d.action for d in res.decisions} | {s: current[s] for s in protected}
    cur_p, new_p = _planning_totals(ctxs2, current, pw2), _planning_totals(ctxs2, new, pw2)
    cur_e, new_e = _eval_policy(tc, ctxs2, current, ew2), _eval_policy(tc, ctxs2, new, ew2)
    changes = _diff(tc, current, new)
    return {
        **tc.plan_meta(),
        "preview_settings": {"budget": bud, "hero_skus_set": heroes, "standard_skus_set": stds,
                             "protected_skus": sorted(protected), "protected_cash": _r(p_cash),
                             "hero_skus_in_preview": [c.sku_id for c in ctxs2 if c.priority == "hero"]},
        "current_plan": {"planning_world": cur_p, "evaluation_world": cur_e},
        "preview_plan": {"planning_world": new_p, "evaluation_world": new_e},
        "changes_vs_current": {
            "planning_world": _delta(new_p, cur_p, ("expected_loss", "service_penalty", "objective", "cash_used")),
            "evaluation_world": _delta(new_e, cur_e, ("total_loss", "units_short", "expired_units")) |
            _delta(new_e, cur_e, ("fill_rate", "hero_fill_rate"), 4)},
        "n_changed_skus": len(changes), "changed_actions": changes[:MAX_ROWS],
        "note": "Preview only; nothing is saved. Both plans are scored with the preview's priorities "
                "(the current plan keeps its decisions). Fill-rate changes are fractions (0.01 = 1 percentage point).",
    }


def budget_frontier(tc: ToolContext, budgets: list[float] | None = None) -> dict:
    if not budgets:
        s = (tc.plan.summary or {}) if tc.plan else {}
        rows = s.get("frontier") or []
        if rows:
            keep = ("budget", "cash_used", "planning_loss", "eval_loss", "eval_fill_rate", "eval_hero_fill_rate",
                    "n_actions", "is_current")
            return {**tc.plan_meta(), "source": "stored with the latest plan",
                    "rows": [{k: r.get(k) for k in keep} for r in rows][:MAX_ROWS],
                    "how_to_read": "planning_loss = expected loss in the planning world; eval_loss / fill rates = replay "
                                   "in the evaluation world. Pass budgets=[...] to compare specific amounts."}
        budgets = _budgets_for(tc.budget)
    bs = [_bounded_budget(b, "each budget") for b in list(budgets)[:5]]
    menu = prepare_menu(tc.ctxs, tc.pw, sorted(set(bs) | set(_budgets_for(tc.budget))), tc.z)
    rows, acts_by = [], []
    for b in bs:
        r = solve_menu(menu, b)
        acts = {a.sku_id: a for a in r.actions}
        ev = _eval_policy(tc, tc.ctxs, acts, tc.ew)
        rows.append({"budget": b, "cash_used": _r(r.cash_used), "planning_expected_loss": _r(r.expected_loss_plan),
                     "planning_service_penalty": _r(r.service_penalty_plan),
                     "eval_total_loss": ev["total_loss"], "eval_fill_rate": ev["fill_rate"],
                     "eval_hero_fill_rate": ev["hero_fill_rate"],
                     "n_actions": sum(1 for a in r.actions if a.kind != "defer"),
                     "actions": [_action_brief(a, tc) for a in r.actions if a.kind != "defer"][:MAX_ROWS]})
        acts_by.append(acts)
    comps = []
    for i in range(1, len(rows)):
        a, b = rows[0], rows[i]
        comps.append({"from_budget": a["budget"], "to_budget": b["budget"],
                      "budget_change": _r(b["budget"] - a["budget"]), "cash_used_change": _r(b["cash_used"] - a["cash_used"]),
                      "planning_expected_loss_change": _r(b["planning_expected_loss"] - a["planning_expected_loss"]),
                      "eval_total_loss_change": _r(b["eval_total_loss"] - a["eval_total_loss"]),
                      "eval_fill_rate_change": None if a["eval_fill_rate"] is None or b["eval_fill_rate"] is None
                      else round(b["eval_fill_rate"] - a["eval_fill_rate"], 4),
                      "eval_hero_fill_rate_change": None if a["eval_hero_fill_rate"] is None or b["eval_hero_fill_rate"] is None
                      else round(b["eval_hero_fill_rate"] - a["eval_hero_fill_rate"], 4),
                      "changed_actions": _diff(tc, acts_by[0], acts_by[i])[:MAX_ROWS]})
    return {**tc.plan_meta(), "source": "computed now (same menu and worlds as the plan)", "rows": rows,
            "comparisons_vs_first": comps,
            "how_to_read": "Changes are later budget minus first budget (negative loss change = less loss). "
                           "Fill-rate changes are fractions (0.01 = 1 percentage point). Nothing is saved."}


def run_scenario(tc: ToolContext, description: str | None = None, demand_multiplier: float | None = None,
                 affected_skus: list[str] | None = None, lead_time_delta_days: int | None = None,
                 delay_supplier: str | None = None, supplier_outage: str | None = None,
                 start_day: int | None = None, end_day: int | None = None, budget: float | None = None) -> dict:
    spec_in: dict = {"summary": str(description or "")[:200],
                     "affected_skus": [tc.sku(s).sku_id for s in (affected_skus or [])][:30]}
    for k, v in (("demand_multiplier", demand_multiplier), ("lead_time_delta_days", lead_time_delta_days),
                 ("start_day", start_day), ("end_day", end_day), ("budget", budget)):
        if v is not None:
            spec_in[k] = v
    if delay_supplier:
        spec_in["delay_supplier"] = tc.supplier(delay_supplier)
    if supplier_outage:
        spec_in["supplier_outage"] = tc.supplier(supplier_outage)
    try:
        spec = scen.validate_spec(spec_in, set(tc.by_id), set(tc.snap.suppliers))
    except ValueError as exc:
        raise ToolError(f"invalid scenario: {str(exc)[:300]}")
    if spec == scen.ScenarioSpec(summary=spec.summary):
        raise ToolError("the scenario changes nothing: set demand_multiplier, lead_time_delta_days, supplier_outage "
                        "or budget")
    bud = float(spec.budget) if spec.budget is not None else tc.budget
    spw, sew = scen.apply(tc.pw, tc.ctxs, spec), scen.apply(tc.ew, tc.ctxs, spec)
    current = tc.plan_actions
    adapted_r = solve_menu(prepare_menu(tc.ctxs, spw, _budgets_for(bud), tc.z), bud)
    adapted = {a.sku_id: a for a in adapted_r.actions}
    cur_base = _eval_policy(tc, tc.ctxs, current, tc.ew)
    cur_s, new_s = _eval_policy(tc, tc.ctxs, current, sew), _eval_policy(tc, tc.ctxs, adapted, sew)
    cur_ps, new_ps = _planning_totals(tc.ctxs, current, spw), _planning_totals(tc.ctxs, adapted, spw)
    changes = _diff(tc, current, adapted)
    cur_ps_sku = value.evaluate_actions(tc.ctxs, [current[c.sku_id] for c in tc.ctxs], tc.ew)["per_sku"]
    cur_ss_sku = value.evaluate_actions(tc.ctxs, [current[c.sku_id] for c in tc.ctxs], sew)["per_sku"]
    hit = []
    for c in tc.ctxs:
        a, b = cur_ps_sku[c.sku_id], cur_ss_sku[c.sku_id]
        dl = b["total_loss"] - a["total_loss"]
        if abs(dl) > 0.5 or abs(b["units_short"] - a["units_short"]) > 0.05:
            hit.append({"sku_id": c.sku_id, "sku_name": c.name, "supplier_id": c.supplier_id,
                        "loss_without_scenario": a["total_loss"], "loss_in_scenario": b["total_loss"],
                        "loss_change": _r(dl), "units_short_change": _r(b["units_short"] - a["units_short"], 1)})
    hit.sort(key=lambda x: -abs(x["loss_change"]))
    return {
        **tc.plan_meta(),
        "spec": spec.model_dump(),
        "adapted_budget": bud,
        "evaluation_world": {
            "current_plan_without_scenario": cur_base, "current_plan_in_scenario": cur_s,
            "adapted_plan_in_scenario": new_s,
            "scenario_impact_on_current_plan": _delta(cur_s, cur_base, ("total_loss", "units_short", "expired_units"))
            | _delta(cur_s, cur_base, ("fill_rate", "hero_fill_rate"), 4),
            "adapted_vs_current_in_scenario": _delta(new_s, cur_s, ("total_loss", "units_short", "expired_units"))
            | _delta(new_s, cur_s, ("fill_rate", "hero_fill_rate"), 4)},
        "planning_world_in_scenario": {"current_plan": cur_ps, "adapted_plan": new_ps,
                                       "adapted_vs_current": _delta(new_ps, cur_ps, ("expected_loss", "objective",
                                                                                     "cash_used"))},
        "n_changed_skus": len(changes), "changed_actions": changes[:MAX_ROWS],
        "current_plan_most_affected_skus": hit[:8],
        "how_to_read": "Days are counted from the as-of date (day 0). Changes are adapted/scenario minus current "
                       "(negative loss change = less loss). Nothing is saved.",
    }


def list_alerts(tc: ToolContext, alert_type: str | None = None) -> dict:
    q = select(Alert).where(Alert.active.is_(True)).order_by(Alert.severity, Alert.entity_id)
    if alert_type:
        q = q.where(Alert.alert_type == str(alert_type))
    rows = list(tc.db.scalars(q))
    out = []
    for a in rows[:15]:
        det = dict(a.details or {})
        if a.alert_type == "supplier_reliability":
            det = {k: det.get(k) for k in ("on_time_rate", "p80_delay_days", "mean_delay_days", "n_receipts")}
        out.append({"entity_id": a.entity_id, "alert_type": a.alert_type, "severity": a.severity,
                    "message": a.message, "details": det})
    return {"count": len(rows), "alerts": out,
            "alert_types": ["stockout_before_arrival", "expiry", "overstock", "supplier_reliability"]}


def list_stockouts(tc: ToolContext) -> dict:
    """Run-out dates from the planning world, i.e. the same numbers the plan screen shows, earliest first."""
    rows = []
    for c in tc.ctxs:
        sw = tc.pw.skus[c.sku_id]
        d = evaluate(c, defer(c), sw)
        if d.first_stockout_day is None:
            continue
        a = tc.plan_actions[c.sku_id]
        p = d if a.kind == "defer" else evaluate(c, a, sw)
        arrivals = sorted(i.arrival_day for i in sw.open_inbound if i.arrival_day is not None)
        rows.append((d.first_stockout_day, {
            "sku_id": c.sku_id, "name": c.name, "priority": c.priority,
            "run_out_date_without_action": tc.day(d.first_stockout_day),
            "units_short_without_action": round(d.units_short),
            "next_open_po_arrival_date": tc.day(next((x for x in arrivals if x > d.first_stockout_day), None)),
            "plan_action": _describe(a),
            **({"new_order_arrival_date": tc.day(sw.expedite_arrival_day if a.expedite else sw.standard_arrival_day)}
               if a.order_qty else {}),
            "run_out_date_with_plan": tc.day(p.first_stockout_day),
            "units_short_with_plan": round(p.units_short)}))
    rows.sort(key=lambda x: (x[0], x[1]["sku_id"]))
    return {**tc.plan_meta(), "as_of": tc.snap.as_of.isoformat(), "count": len(rows),
            "how_to_read": "Sorted by run-out date without action (the first day stock is likely to run out across "
                           "the forecast range and supplier-delay cases; the plan screen shows the same date). "
                           "run_out_date_with_plan null = the plan covers it.",
            "stockouts": [r for _, r in rows[:20]]}


SIGNAL_STATUSES = ("pending", "confirmed", "rejected", "invalid")


def list_signals(tc: ToolContext, status: str | None = None) -> dict:
    q = select(SignalEvent).order_by(SignalEvent.id.desc())
    if status:
        if status not in SIGNAL_STATUSES:
            raise ToolError(f"status must be one of {list(SIGNAL_STATUSES)}")
        q = q.where(SignalEvent.status == status)
    rows = list(tc.db.scalars(q.limit(50)))
    return {"count": len(rows), "signals": [
        {"ref": f"EV-{e.id}", "event_type": e.event_type, "sku_ids": list(e.sku_ids or [])[:8],
         "magnitude": e.magnitude, "start_date": e.start_date.isoformat() if e.start_date else None,
         "end_date": e.end_date.isoformat() if e.end_date else None, "status": e.status,
         "quoted_evidence": (e.quoted_evidence or "")[:160],
         **({"rejection_reason": e.rejection_reason[:120]} if e.rejection_reason else {})} for e in rows[:MAX_ROWS]]}


EMAIL_PURPOSES = ("expedite", "new_order", "delay_follow_up", "general")


def draft_supplier_email(tc: ToolContext, supplier_id: str, purpose: str | None = None) -> dict:
    sid = tc.supplier(supplier_id)
    purpose = purpose or "general"
    if purpose not in EMAIL_PURPOSES:
        raise ToolError(f"purpose must be one of {list(EMAIL_PURPOSES)}")
    sup = tc.snap.suppliers[sid]
    approved = tc.db.scalars(select(Plan).where(Plan.status == "approved").order_by(Plan.id.desc()).limit(1)).first()
    plan = approved or tc.plan
    products = tc.snap.products
    lines = []
    for r in (plan.actions if plan else []):
        p = products.get(r.get("sku_id"))
        if (r.get("supplier_id") or (p.supplier_id if p else None)) != sid or not r.get("order_qty"):
            continue
        lines.append({"sku_id": r["sku_id"], "sku_name": r.get("sku_name"), "order_qty": r["order_qty"],
                      "expedite": bool(r.get("expedite")), "requested_arrival_date": r.get("arrival_date"),
                      "unit_cost": p.unit_cost if p else None, "line_cash": r.get("cash_outlay"),
                      "expedite_premium": r.get("expedite_premium")})
    pos = [{"po_id": o.po_id, "sku_id": o.sku_id, "sku_name": products[o.sku_id].name, "qty": o.qty,
            "order_date": o.order_date.isoformat(), "expected_date": o.expected_date.isoformat()}
           for v in tc.snap.open_pos.values() for o in v if o.supplier_id == sid]
    delays = [{"ref": f"EV-{e.id}", "sku_ids": list(e.sku_ids or [])[:8], "delay_days": e.magnitude}
              for e in tc.snap.confirmed if e.event_type == "supplier_delay"
              and any(products[s].supplier_id == sid for s in (e.sku_ids or []) if s in products)]
    m = tc.snap.metrics.get(sid)
    out = {
        "draft_only": True,
        "purpose": purpose,
        "supplier": {"supplier_id": sid, "name": sup.name, "terms": sup.terms,
                     "stated_lead_time_days": sup.stated_lead_time_days,
                     "expedite_lead_time_days": sup.expedite_lead_time_days,
                     "expedite_premium_pct": sup.expedite_premium_pct,
                     "on_time_rate": m.on_time_rate if m else None, "p80_delay_days": m.p80_delay_days if m else None},
        "plan": None if plan is None else {"plan_id": plan.id, "status": plan.status, "as_of": plan.as_of.isoformat(),
                                           "approved_at": plan.approved_at.isoformat() if plan.approved_at else None},
        "plan_lines": lines[:MAX_ROWS], "expedited_lines": sum(1 for l in lines if l["expedite"]),
        "open_pos": pos[:MAX_ROWS], "confirmed_delay_signals": delays[:6],
        "instructions": "Write the email using only these facts. It is a draft for the operator; nothing is sent.",
    }
    if approved is None:
        out["plan_note"] = "No plan is approved yet; facts come from the latest draft plan."
    if purpose == "expedite" and not out["expedited_lines"]:
        out["expedite_note"] = "The plan has no expedited lines for this supplier; list the standard lines and open POs instead."
    return out


# ---------------------------------------------------------------- registry

_SKU = {"type": "string", "description": "SKU id, e.g. SKU-015"}
_SKUS = {"type": "array", "items": {"type": "string"}, "description": "SKU ids, e.g. [\"SKU-001\"]"}


def _obj(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or []}


def build_tools(tc: ToolContext) -> list[ToolSpec]:
    def bind(fn):
        return lambda **kw: fn(tc, **kw)

    return [
        ToolSpec("get_plan_summary",
                 "Latest recovery plan: budget, cash used, expected loss with/without the plan, savings and fill rates "
                 "vs the static and tuned reorder-rule baselines, robustness (Monte Carlo) and the action list "
                 "(SKU, kind, quantity, cash, loss avoided, reasons).", _obj({}), bind(get_plan_summary)),
        ToolSpec("list_skus", "Find SKUs by name, category, supplier or priority (hero/standard), with stock, open "
                 "PO quantity, earliest expiry and the plan's action.",
                 _obj({"name_contains": {"type": "string"}, "category": {"type": "string"},
                       "supplier_id": {"type": "string", "description": "e.g. SUP-04"},
                       "priority": {"type": "string", "enum": ["hero", "standard"]}}), bind(list_skus)),
        ToolSpec("get_sku", "Everything about one SKU: lots and expiry, open POs, 30-day forecast p10/p50/p90 totals, "
                 "supplier reliability and lead times, priority, reorder math, outcome with no action, the plan's "
                 "action and the alternatives the optimiser considered (incl. expedite / markdown options with cash "
                 "and objective). Use it for 'why (not) X for SKU-...' questions.",
                 _obj({"sku_id": _SKU}, ["sku_id"]), bind(get_sku)),
        ToolSpec("evaluate_alternative",
                 "Score one alternative action for a SKU against the plan's action (planning and evaluation worlds, "
                 "cash, budget fit). Unspecified fields keep the plan's values. Nothing is saved.",
                 _obj({"sku_id": _SKU,
                       "order_qty": {"type": "number", "description": "units to order (0 = no order); rounded to MOQ/case pack"},
                       "expedite": {"type": "boolean", "description": "expedite the order"},
                       "discount_pct": {"type": "number", "description": "markdown as a fraction, 0.05-0.6 (0 removes it)"},
                       "markdown_lot_id": {"type": "string", "description": "lot to mark down (default: soonest expiry)"},
                       "markdown_start_day": {"type": "integer", "description": "day the markdown starts (0 = today)"},
                       "skip": {"type": "boolean", "description": "no action for this SKU"}}, ["sku_id"]),
                 bind(evaluate_alternative)),
        ToolSpec("replan_preview",
                 "Re-optimise the whole plan in memory with a different budget and/or priority changes and report what "
                 "changes vs the current plan (actions, cash, expected loss, fill and hero fill rates). protect_hero / "
                 "protect_skus keep the current plan's actions for those SKUs fixed. Nothing is saved.",
                 _obj({"budget": {"type": "number", "description": "INR, e.g. 800000 (8 lakh)"},
                       "hero_skus": {**_SKUS, "description": "SKUs to treat as hero (priority)"},
                       "standard_skus": {**_SKUS, "description": "SKUs to treat as standard"},
                       "protect_skus": {**_SKUS, "description": "keep the current plan's action for these SKUs"},
                       "protect_hero": {"type": "boolean", "description": "keep current actions of all hero SKUs"}}),
                 bind(replan_preview)),
        ToolSpec("budget_frontier",
                 "Budget vs outcome. Without arguments: the frontier stored with the plan. With budgets=[...] "
                 "(up to 5, INR): solve the plan at each budget and compare each with the first (cash, loss, fill "
                 "rates, which actions change). Use for 'compare 50k vs 75k'.",
                 _obj({"budgets": {"type": "array", "items": {"type": "number"}}}), bind(budget_frontier)),
        ToolSpec("run_scenario",
                 "What-if: transform demand / lead times / supplier availability and compare the current plan with a "
                 "plan re-optimised for the scenario. Example 'SUP-04 slips another week': delay_supplier='SUP-04', "
                 "lead_time_delta_days=7. Nothing is saved.",
                 _obj({"description": {"type": "string", "description": "one-line restatement of the scenario"},
                       "demand_multiplier": {"type": "number", "description": "e.g. 1.2 = +20% demand (0-5)"},
                       "affected_skus": {**_SKUS, "description": "SKUs the demand change applies to (empty = all)"},
                       "lead_time_delta_days": {"type": "integer", "description": "extra days of delay (-10..60)"},
                       "delay_supplier": {"type": "string", "description": "supplier whose deliveries slip"},
                       "supplier_outage": {"type": "string", "description": "supplier that cannot deliver"},
                       "start_day": {"type": "integer", "description": "first affected day (0-29)"},
                       "end_day": {"type": "integer", "description": "end day, exclusive (1-30)"},
                       "budget": {"type": "number", "description": "budget for the adapted plan (default: plan budget)"}}),
                 bind(run_scenario)),
        ToolSpec("list_stockouts",
                 "Which products will run out and when, earliest first: run-out date and units short without action "
                 "and with the current plan, next open PO arrival, the plan's action. Same dates as the plan screen. "
                 "Use it for 'what runs out first' / 'when will X run out'.", _obj({}), bind(list_stockouts)),
        ToolSpec("list_alerts", "Active alerts: stockout before arrival, expiry risk (lots and write-off), overstock, "
                 "supplier reliability.",
                 _obj({"alert_type": {"type": "string", "enum": ["stockout_before_arrival", "expiry", "overstock",
                                                                  "supplier_reliability"]}}), bind(list_alerts)),
        ToolSpec("list_signals", "Signals extracted from supplier/campaign messages (EV-ids) with status and quoted "
                 "evidence.", _obj({"status": {"type": "string", "enum": list(SIGNAL_STATUSES)}}), bind(list_signals)),
        ToolSpec("draft_supplier_email",
                 "Facts for an email to a supplier (plan lines with quantities and requested arrival dates, open POs, "
                 "delay signals) from the approved plan, else the latest draft. You write the email from these facts; "
                 "it is a draft only and is never sent.",
                 _obj({"supplier_id": {"type": "string", "description": "e.g. SUP-04"},
                       "purpose": {"type": "string", "enum": list(EMAIL_PURPOSES)}}, ["supplier_id"]),
                 bind(draft_supplier_email)),
    ]


# ---------------------------------------------------------------- summaries (UI + deterministic fallback)

def _inr(v: Any) -> str:
    return "n/a" if v is None else f"INR {float(v):,.0f}"


def _pct(v: Any) -> str:
    return "n/a" if v is None else f"{float(v) * 100:.1f}%"


def summarize(name: str, r: dict) -> str:
    if "error" in r:
        return f"error: {r['error']}"
    try:
        if name == "get_plan_summary":
            if r.get("plan_id") is None:
                return r.get("note", "no plan")
            ev = r.get("evaluation_world") or {}
            return (f"plan #{r['plan_id']} ({r.get('plan_status')}): {r.get('n_actions')} actions, cash "
                    f"{_inr(r.get('cash_used'))} of {_inr(r.get('budget'))}; simulated loss {_inr(ev.get('plan_total_loss'))} "
                    f"vs static baseline {_inr(ev.get('static_baseline_total_loss'))} (savings "
                    f"{_inr(ev.get('savings_vs_static_baseline'))}), fill rate {_pct(ev.get('plan_fill_rate'))}")
        if name == "list_skus":
            return f"{r['count']} SKUs: " + ", ".join(f"{s['sku_id']} {s['name']}" for s in r["skus"][:6]) + \
                ("…" if r["count"] > 6 else "")
        if name == "get_sku":
            pa = r["plan_action"]
            return (f"{r['sku_id']} {r['name']} ({r['priority']['priority']}): plan = {pa['action']} "
                    f"(cash {_inr(pa['cash'])}, avoids {_inr(pa['loss_avoided'])}); {len(r['alternatives_considered'])} "
                    f"alternatives shown")
        if name == "evaluate_alternative":
            pw = r["planning_world"]
            return (f"{r['sku_id']}: {r['alternative']['action']} vs plan {r['plan_action']['action']}: expected loss "
                    f"change {_inr(pw['expected_loss_change'])}, cash change {_inr(r['cash']['cash_change'])}, "
                    f"fits budget: {r['cash']['fits_budget']}")
        if name == "replan_preview":
            ch = r["changes_vs_current"]
            return (f"budget {_inr(r['preview_settings']['budget'])}: {r['n_changed_skus']} SKUs change; expected loss "
                    f"change {_inr(ch['planning_world']['expected_loss_change'])}, cash change "
                    f"{_inr(ch['planning_world']['cash_used_change'])}, simulated loss change "
                    f"{_inr(ch['evaluation_world']['total_loss_change'])}")
        if name == "budget_frontier":
            return "; ".join(f"{_inr(x['budget'])}: cash {_inr(x.get('cash_used'))}, simulated loss "
                             f"{_inr(x.get('eval_total_loss', x.get('eval_loss')))}, fill {_pct(x.get('eval_fill_rate'))}"
                             for x in r["rows"][:5])
        if name == "run_scenario":
            ev = r["evaluation_world"]
            return (f"current plan loss in scenario {_inr(ev['current_plan_in_scenario']['total_loss'])}, adapted plan "
                    f"{_inr(ev['adapted_plan_in_scenario']['total_loss'])}; {r['n_changed_skus']} SKUs change")
        if name == "list_stockouts":
            return f"{r['count']} products run out without action, earliest first: " + "; ".join(
                f"{s['name']} {s['run_out_date_without_action']}" for s in r["stockouts"][:6])
        if name == "list_alerts":
            return f"{r['count']} active alerts: " + "; ".join(f"{a['alert_type']} {a['entity_id']}"
                                                               for a in r["alerts"][:6])
        if name == "list_signals":
            return f"{r['count']} signals: " + "; ".join(f"{s['ref']} {s['event_type']} ({s['status']})"
                                                         for s in r["signals"][:6])
        if name == "draft_supplier_email":
            return (f"{r['supplier']['supplier_id']} {r['supplier']['name']}: {len(r['plan_lines'])} plan lines "
                    f"({r['expedited_lines']} expedited), {len(r['open_pos'])} open POs (draft only)")
    except (KeyError, TypeError, IndexError):
        pass
    return "ok"

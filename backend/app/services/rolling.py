"""Rolling re-planning (weekly review) simulated in an evaluation world.

Plan-once commits today's decisions and never revisits them. Weekly re-planning re-runs the optimiser on days
7, 14 and 21 with the stock that actually remains, the POs still in transit, the cash left in the budget and a
statistical re-forecast that includes the demand realised so far. The rule-based baseline is also run weekly, so
the comparison isolates the decision logic rather than the review cadence.

Simplifications (documented in the scorecard): realised *demand* is appended to history (not censored sales);
an overdue open PO is assumed by the planner to arrive per the supplier delay cases from its original date.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import timedelta
from typing import Callable, Sequence

import numpy as np

from . import forecasting as fc
from . import state
from .planner import baseline_actions, build_plan
from .simulator import Inbound, Lot, Markdown, markdown_uplift, simulate
from .value import _stats
from .world import CANNIBALISATION_SHARE, Action, ForecastBand, OpenPO, SkuContext, World

REVIEW_DAYS = (0, 7, 14, 21)


@dataclass
class PipeItem:
    ref: str
    sku_id: str
    qty: float
    true_arrival: int | None  # absolute day; None = never within the horizon
    expected: int  # absolute day the planner was promised


@dataclass
class Truth:
    demand: dict[str, np.ndarray]
    open_po_arrival: dict[str, int | None]
    new_po_delay: dict[str, int]


def truth_from_world(world: World, ctxs: Sequence[SkuContext]) -> Truth:
    demand, arrivals, delays = {}, {}, {}
    for ctx in ctxs:
        sw = world.skus[ctx.sku_id]
        demand[ctx.sku_id] = np.asarray(sw.demand_paths[0][1], dtype=float)
        for i in sw.open_inbound:
            arrivals[i.ref_id] = i.arrival_day
        if sw.standard_arrival_day is not None:
            delays[ctx.supplier_id] = max(0, sw.standard_arrival_day - ctx.stated_lead_time)
    return Truth(demand, arrivals, delays)


Policy = Callable[[int, list[SkuContext], float], list[Action]]


def simulate_policy(snap: state.Snapshot, ctxs: Sequence[SkuContext], truth: Truth, budget: float,
                    policy: Policy, review_days: Sequence[int] = REVIEW_DAYS) -> dict:
    H = snap.horizon
    lots = {c.sku_id: list(c.lots) for c in ctxs}
    pipe = [PipeItem(o.po_id, c.sku_id, o.qty, truth.open_po_arrival.get(o.po_id, o.expected_day), o.expected_day)
            for c in ctxs for o in c.open_pos]
    active_md: dict[str, Markdown] = {}  # start_day absolute
    spent = 0.0
    acc = {"lost_margin": 0.0, "expiry_writeoff": 0.0, "markdown_cost": 0.0, "cannibalisation": 0.0,
           "expedite_premium": 0.0, "units_short": 0.0, "demand_units": 0.0, "hero_short": 0.0, "hero_demand": 0.0}
    n_orders = 0
    reviews = [d for d in review_days if d < H]
    for k, t in enumerate(reviews):
        end = reviews[k + 1] if k + 1 < len(reviews) else H
        ctxs_t = []
        for c in ctxs:
            hist = np.concatenate([np.asarray(c.history, dtype=float), truth.demand[c.sku_id][:t]])
            ctxs_t.append(replace(c, lots=lots[c.sku_id], history=hist,
                                  open_pos=[OpenPO(p.ref, p.qty, max(1, p.expected - t), c.supplier_id)
                                            for p in pipe if p.sku_id == c.sku_id]))
        actions = policy(t, ctxs_t, max(0.0, budget - spent))
        for c, a in zip(ctxs_t, actions):
            if a.kind == "defer":
                continue
            spent += a.cash_outlay
            acc["expedite_premium"] += a.expedite_premium
            if a.order_qty > 0:
                n_orders += 1
                lt = c.expedite_lead_time if a.expedite else c.stated_lead_time
                delay = 0 if a.expedite else truth.new_po_delay.get(c.supplier_id, 0)
                pipe.append(PipeItem(f"NEW-{c.sku_id}-D{t}", c.sku_id, float(a.order_qty), t + lt + delay, t + lt))
            if a.markdown is not None:
                active_md[c.sku_id] = Markdown(a.markdown.lot_id, a.markdown.discount_pct, t + a.markdown.start_day)
        L = end - t
        for c in ctxs:
            inbound = [Inbound(p.ref, p.qty, p.true_arrival - t) for p in pipe
                       if p.sku_id == c.sku_id and p.true_arrival is not None and t <= p.true_arrival < end]
            md = active_md.get(c.sku_id)
            seg_md = None if md is None else Markdown(md.lot_id, md.discount_pct, max(0, md.start_day - t))
            r = simulate(c.econ, lots[c.sku_id], inbound, truth.demand[c.sku_id][t:end], seg_md,
                         None if seg_md is None else markdown_uplift(seg_md.discount_pct, c.category))
            acc["lost_margin"] += r.lost_margin
            acc["expiry_writeoff"] += r.expiry_writeoff
            acc["markdown_cost"] += r.markdown_cost
            acc["cannibalisation"] += CANNIBALISATION_SHARE * r.incremental_units * c.substitute_margin
            acc["units_short"] += r.unmet_units
            acc["demand_units"] += r.demand_units
            if c.priority == "hero":
                acc["hero_short"] += r.unmet_units
                acc["hero_demand"] += r.demand_units
            lots[c.sku_id] = [Lot(l.lot_id, l.qty, l.expiry_day - L) for l in r.final_lots]
        pipe = [p for p in pipe if not (p.true_arrival is not None and p.true_arrival < end)]
    total = sum(acc[k] for k in ("lost_margin", "expiry_writeoff", "markdown_cost", "cannibalisation",
                                 "expedite_premium"))
    return {
        "total_loss": round(total, 2),
        **{k: round(acc[k], 2) for k in ("lost_margin", "expiry_writeoff", "markdown_cost", "cannibalisation",
                                         "expedite_premium", "units_short", "demand_units")},
        "cash_spent": round(spent, 2),
        "orders_placed": n_orders,
        "fill_rate": round(1 - acc["units_short"] / acc["demand_units"], 4) if acc["demand_units"] else None,
        "hero_fill_rate": round(1 - acc["hero_short"] / acc["hero_demand"], 4) if acc["hero_demand"] else None,
    }


# ---------------------------------------------------------------- policies

def plan_once(first: Sequence[Action]) -> Policy:
    first = list(first)

    def policy(t: int, ctxs_t: list[SkuContext], remaining: float) -> list[Action]:
        if t == 0:
            return first
        return [Action(c.sku_id, "defer", c.econ.unit_cost, c.expedite_premium_pct) for c in ctxs_t]
    return policy


def replan_weekly(snap: state.Snapshot, first: Sequence[Action], z: float) -> Policy:
    first = list(first)

    def policy(t: int, ctxs_t: list[SkuContext], remaining: float) -> list[Action]:
        if t == 0:
            return first
        snap_t = replace(snap, as_of=snap.as_of + timedelta(days=t), horizon=snap.horizon - t)
        ctxs_f = []
        for c in ctxs_t:
            p10, p50, p90, drivers, _ = fc.statistical_forecast(c.history, snap_t.as_of, snap_t.horizon,
                                                                state.demand_events(snap, c.sku_id))
            ctxs_f.append(replace(c, forecast=ForecastBand(p10, p50, p90, "statistical", None, drivers)))
        pw = state.planning_world(snap_t, ctxs_f)
        return build_plan(ctxs_f, pw, remaining, z).actions
    return policy


def baseline_weekly() -> Policy:
    def policy(t: int, ctxs_t: list[SkuContext], remaining: float) -> list[Action]:
        return baseline_actions(ctxs_t, remaining)
    return policy


def compare_rolling(snap: state.Snapshot, ctxs: Sequence[SkuContext], worlds: Sequence[World], plan: Sequence[Action],
                    budget: float, z: float) -> dict:
    """Plan-once vs weekly re-plan vs weekly baseline, in the actual world (worlds[0]) and across the rest."""
    policies = {"plan_once": lambda: plan_once(plan), "replan_weekly": lambda: replan_weekly(snap, plan, z),
                "baseline_weekly": baseline_weekly}
    runs: dict[str, list[dict]] = {k: [] for k in policies}
    for w in worlds:
        truth = truth_from_world(w, ctxs)
        for name, make in policies.items():
            runs[name].append(simulate_policy(snap, ctxs, truth, budget, make()))
    out = {"review_days": list(REVIEW_DAYS), "actual_world": {k: v[0] for k, v in runs.items()},
           "n_mc_worlds": len(worlds) - 1, "mc": {}, "comparisons": {}}
    if len(worlds) > 1:
        for name, rs in runs.items():
            out["mc"][name] = {"total_loss": _stats([r["total_loss"] for r in rs[1:]]),
                               "fill_rate": _stats([r["fill_rate"] or 0 for r in rs[1:]])}
        ref = np.array([r["total_loss"] for r in runs["replan_weekly"][1:]])
        for other in ("plan_once", "baseline_weekly"):
            diff = np.array([r["total_loss"] for r in runs[other][1:]]) - ref
            out["comparisons"][f"replan_weekly_vs_{other}"] = {**_stats(diff),
                                                               "win_rate": round(float(np.mean(diff > 0)), 4)}
    out["notes"] = ("Weekly reviews on days 0/7/14/21 within the same 30-day window. Re-plans use a statistical "
                    "re-forecast with realised demand appended, remaining budget, stock and in-transit POs.")
    return out

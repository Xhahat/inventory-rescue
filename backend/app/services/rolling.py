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
# Decides, at the review where a scheduled tranche falls due, whether to place it as scheduled (True) or release
# its cash back to that review's re-plan (False). None = always place (plan-once follows its schedule).
Confirm = Callable[[int, SkuContext, int], bool]


@dataclass
class Scheduled:
    sku_id: str
    qty: int
    due: int  # absolute day the PO is placed
    cash: float


def simulate_policy(snap: state.Snapshot, ctxs: Sequence[SkuContext], truth: Truth, budget: float,
                    policy: Policy, review_days: Sequence[int] = REVIEW_DAYS, confirm: Confirm | None = None,
                    capital_rate: float = 0.0) -> dict:
    H = snap.horizon
    lots = {c.sku_id: list(c.lots) for c in ctxs}
    pipe = [PipeItem(o.po_id, c.sku_id, o.qty, truth.open_po_arrival.get(o.po_id, o.expected_day), o.expected_day)
            for c in ctxs for o in c.open_pos]
    active_md: dict[str, Markdown] = {}  # start_day absolute
    scheduled: list[Scheduled] = []
    spent = 0.0
    spent_day0 = 0.0
    capital = 0.0
    acc = {"lost_margin": 0.0, "expiry_writeoff": 0.0, "markdown_cost": 0.0, "cannibalisation": 0.0,
           "expedite_premium": 0.0, "units_short": 0.0, "demand_units": 0.0, "hero_short": 0.0, "hero_demand": 0.0,
           "service_penalty": 0.0}
    n_orders = n_released = 0
    reviews = [d for d in review_days if d < H]
    by_id = {c.sku_id: c for c in ctxs}

    def contexts(t: int) -> list[SkuContext]:
        out = []
        for c in ctxs:
            hist = np.concatenate([np.asarray(c.history, dtype=float), truth.demand[c.sku_id][:t]])
            out.append(replace(c, lots=lots[c.sku_id], history=hist,
                               open_pos=[OpenPO(p.ref, p.qty, max(1, p.expected - t), c.supplier_id)
                                         for p in pipe if p.sku_id == c.sku_id]))
        return out

    def place(t: int, c: SkuContext, qty: float, expedite: bool, ref: str) -> None:
        lt = c.expedite_lead_time if expedite else c.stated_lead_time
        delay = 0 if expedite else truth.new_po_delay.get(c.supplier_id, 0)
        pipe.append(PipeItem(ref, c.sku_id, float(qty), t + lt + delay, t + lt))

    for k, t in enumerate(reviews):
        end = reviews[k + 1] if k + 1 < len(reviews) else H
        due = [s for s in scheduled if s.due <= t]
        if due:
            scheduled = [s for s in scheduled if s.due > t]
            ctxs_t = {c.sku_id: c for c in contexts(t)}
            for s in due:
                if confirm is None or confirm(t, ctxs_t[s.sku_id], s.qty):
                    place(t, by_id[s.sku_id], s.qty, False, f"NEW2-{s.sku_id}-D{t}")
                    n_orders += 1
                    capital += capital_rate / 365.0 * s.cash * (H - t)
                else:
                    spent -= s.cash
                    n_released += 1
        ctxs_t = contexts(t)
        actions = policy(t, ctxs_t, max(0.0, budget - spent))
        for c, a in zip(ctxs_t, actions):
            if a.kind == "defer":
                continue
            spent += a.cash_outlay
            if t == 0:
                spent_day0 += a.cash_today
            capital += capital_rate / 365.0 * a.cash_today * (H - t)
            acc["expedite_premium"] += a.expedite_premium
            if a.order_qty > 0:
                n_orders += 1
                place(t, c, a.order_qty, a.expedite, f"NEW-{c.sku_id}-D{t}")
            if a.later_qty > 0:
                scheduled.append(Scheduled(c.sku_id, a.later_qty, t + a.later_day, a.cash_later))
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
            acc["service_penalty"] += r.unmet_units * c.goodwill_per_unit
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
                                         "expedite_premium", "units_short", "demand_units", "service_penalty")},
        "loss_incl_service": round(total + acc["service_penalty"], 2),
        "cash_spent": round(spent, 2),
        "cash_day0": round(spent_day0, 2),
        "capital_cost": round(capital, 2),
        "orders_placed": n_orders,
        "tranches_released": n_released,
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


def replan_weekly(snap: state.Snapshot, first: Sequence[Action], z: float, cache: dict | None = None) -> Policy:
    first = list(first)

    def policy(t: int, ctxs_t: list[SkuContext], remaining: float) -> list[Action]:
        if t == 0:
            return first
        snap_t, ctxs_f = _reforecast(snap, t, ctxs_t, cache)
        pw = state.planning_world(snap_t, ctxs_f)
        return build_plan(ctxs_f, pw, remaining, z).actions
    return policy


def _reforecast(snap: state.Snapshot, t: int, ctxs_t: Sequence[SkuContext],
                cache: dict | None = None) -> tuple[state.Snapshot, list[SkuContext]]:
    """Statistical re-forecast at review day t from history + realised demand. `cache` is keyed on the realised
    history, which depends only on the world (not on the policy), so options in the same world share it."""
    snap_t = replace(snap, as_of=snap.as_of + timedelta(days=t), horizon=snap.horizon - t)
    out = []
    for c in ctxs_t:
        key = (c.sku_id, t, hash(np.asarray(c.history, dtype=float).tobytes()))
        band = None if cache is None else cache.get(key)
        if band is None:
            p10, p50, p90, drivers, _ = fc.statistical_forecast(c.history, snap_t.as_of, snap_t.horizon,
                                                                state.demand_events(snap, c.sku_id))
            band = ForecastBand(p10, p50, p90, "statistical", None, drivers)
            if cache is not None:
                cache[key] = band
        out.append(replace(c, forecast=band))
    return snap_t, out


def release_to_replan(t: int, ctx_t: SkuContext, qty: int) -> bool:
    """Under weekly re-planning a scheduled tranche is provisional: on its review day its cash goes back into the
    pool and the re-plan decides with what is known then (re-order the same SKU, resize, or move the cash)."""
    return False


def baseline_weekly() -> Policy:
    def policy(t: int, ctxs_t: list[SkuContext], remaining: float) -> list[Action]:
        return baseline_actions(ctxs_t, remaining)
    return policy


def compare_rolling(snap: state.Snapshot, ctxs: Sequence[SkuContext], worlds: Sequence[World], plan: Sequence[Action],
                    budget: float, z: float) -> dict:
    """Plan-once vs weekly re-plan vs weekly baseline, in the actual world (worlds[0]) and across the rest."""
    cache: dict = {}
    policies = {"plan_once": lambda: (plan_once(plan), None),
                "replan_weekly": lambda: (replan_weekly(snap, plan, z, cache), release_to_replan),
                "baseline_weekly": lambda: (baseline_weekly(), None)}
    runs: dict[str, list[dict]] = {k: [] for k in policies}
    for w in worlds:
        truth = truth_from_world(w, ctxs)
        for name, make in policies.items():
            pol, conf = make()
            runs[name].append(simulate_policy(snap, ctxs, truth, budget, pol, confirm=conf))
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


# ---------------------------------------------------------------- shock worlds and plan options

SHOCK_SKU_SHARE = 0.3
SHOCK_MULTIPLIERS = (0.5, 0.7, 1.4, 1.8)
SHOCK_START_DAYS = (7, 14)  # inclusive range of the day the shift starts
SHOCK_SUPPLIER_SLIP_DAYS = (4, 8)


def shock_worlds(worlds: Sequence[World], ctxs: Sequence[SkuContext], seed: int = 11) -> list[World]:
    """Mid-month surprises on top of calm worlds, i.e. what a day-0 plan cannot know: ~30% of SKUs see demand
    shift by x0.5-x1.8 from a day between 7 and 14 onward, and one supplier's new POs slip by 4-8 extra days."""
    rng = np.random.default_rng(seed)
    sups = sorted({c.supplier_id for c in ctxs})
    out = []
    for w in worlds:
        slip_sup = sups[int(rng.integers(len(sups)))] if sups else None
        slip = int(rng.integers(SHOCK_SUPPLIER_SLIP_DAYS[0], SHOCK_SUPPLIER_SLIP_DAYS[1] + 1))
        skus, shocked = {}, []
        for c in ctxs:
            sw = w.skus[c.sku_id]
            path = np.array(sw.demand_paths[0][1], dtype=float)
            if rng.random() < SHOCK_SKU_SHARE:
                d0 = int(rng.integers(SHOCK_START_DAYS[0], SHOCK_START_DAYS[1] + 1))
                m = float(rng.choice(SHOCK_MULTIPLIERS))
                path[d0:] = np.round(path[d0:] * m)
                shocked.append({"sku_id": c.sku_id, "from_day": d0, "mult": m})
            std = sw.standard_arrival_day
            if c.supplier_id == slip_sup and std is not None:
                std += slip
            skus[c.sku_id] = replace(sw, demand_paths=[(1.0, path)], standard_arrival_day=std, supply_cases=[])
        out.append(World(f"{w.name}-shock", skus, {**w.description, "shocked_skus": shocked,
                                                    "slipped_supplier": slip_sup, "slip_days": slip}))
    return out


def compare_options(snap: state.Snapshot, ctxs: Sequence[SkuContext], actual: World, calm: Sequence[World],
                    shocked: Sequence[World], options: dict[str, tuple[Sequence[Action], float]], z: float,
                    capital_rate: float) -> dict:
    """Run every plan option (day-0 actions, budget) with weekly re-planning of the remaining budget (scheduled
    tranches are released to the re-plan on their review day), in the actual world, calm Monte Carlo worlds and
    shock worlds. Every option sees exactly the same worlds, so differences are paired."""
    cache: dict = {}
    groups = {"actual": [actual], "calm": list(calm), "shock": list(shocked)}
    runs: dict[str, dict[str, list[dict]]] = {k: {g: [] for g in groups} for k in options}
    for g, ws in groups.items():
        for w in ws:
            truth = truth_from_world(w, ctxs)
            for name, (acts, budget) in options.items():
                runs[name][g].append(simulate_policy(snap, ctxs, truth, budget, replan_weekly(snap, acts, z, cache),
                                                     confirm=release_to_replan, capital_rate=capital_rate))
    out: dict = {"n_calm": len(calm), "n_shock": len(shocked), "options": {}, "best": {}, "best_service": {}}
    for name, rs in runs.items():
        row = {"actual": rs["actual"][0]}
        for g in ("calm", "shock"):
            if rs[g]:
                row[g] = {"total_loss": _stats([r["total_loss"] for r in rs[g]]),
                          "loss_incl_service": _stats([r["loss_incl_service"] for r in rs[g]]),
                          "capital_cost": round(float(np.mean([r["capital_cost"] for r in rs[g]])), 2),
                          "fill_rate": round(float(np.mean([r["fill_rate"] or 0 for r in rs[g]])), 4),
                          "hero_fill_rate": round(float(np.mean([r["hero_fill_rate"] or 0 for r in rs[g]])), 4),
                          "cash_spent": round(float(np.mean([r["cash_spent"] for r in rs[g]])), 2),
                          "tranches_released": round(float(np.mean([r["tranches_released"] for r in rs[g]])), 2)}
        out["options"][name] = row
    ref = "commit_now" if "commit_now" in runs else next(iter(runs))
    for name in runs:
        if name == ref:
            continue
        for g in ("calm", "shock"):
            if runs[name][g]:
                diff = np.array([r["total_loss"] for r in runs[ref][g]]) - np.array([r["total_loss"] for r in runs[name][g]])
                out["options"][name].setdefault("vs_commit_now", {})[g] = {
                    **_stats(diff), "win_rate": round(float(np.mean(diff > 0)), 4)}
    for g in ("actual", "calm", "shock"):
        for key, metric in (("best", "total_loss"), ("best_service", "loss_incl_service")):
            scores = {n: (r[g][metric] if g == "actual" else r[g][metric]["mean"])
                      for n, r in out["options"].items() if g in r}
            if scores:
                out[key][g] = min(scores, key=scores.get)
    out["notes"] = ("Each option places its day-0 actions, then re-plans the remaining budget at the day 7/14/21 "
                    "reviews; scheduled tranches are provisional: on their review day the cash returns to the "
                    "re-plan, which re-orders, resizes or redirects it. Calm worlds: Poisson demand around the "
                    "actual path with a small per-SKU shock (they share the actual month's forecast misses). Shock "
                    "worlds: the same worlds plus mid-month demand shifts on ~30% of SKUs and one supplier slipping "
                    "4-8 days. Loss incl. service adds the hero/standard goodwill penalty per unit short. Capital "
                    "cost is reported separately and is not part of total loss.")
    return out

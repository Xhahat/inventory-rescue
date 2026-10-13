"""Candidate actions, exact multi-choice knapsack under a cash budget, and the rule-based baseline."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Sequence

import numpy as np

from .simulator import Markdown
from .supply import ReorderMath, reorder_math, round_order_qty
from .world import Action, Outcome, SkuContext, SkuWorld, World, defer, evaluate

COVER_DAYS = 14
PARTIAL_FRACTIONS = (0.25, 0.5, 0.75)
MARKDOWN_DISCOUNTS = (0.2, 0.35, 0.5)
MARKDOWN_LATE_START_DAYS = 7
MIN_BENEFIT_INR = 1.0


# ---------------------------------------------------------------- knapsack

def solve_mckp(groups: Sequence[Sequence[tuple[float, float]]], budget: float,
               granularity: float | None = None) -> tuple[list[int], float]:
    """Exact multiple-choice knapsack: pick exactly one (cost, loss) option per group, minimising total loss
    with total cost <= budget. Costs are rounded *up* to `granularity` so the true budget is never exceeded;
    the optimum is exact with respect to those rounded costs. Every group must contain a zero-cost option.
    Ties are broken towards lower cost."""
    if budget < 0:
        raise ValueError("budget must be >= 0")
    g = float(granularity) if granularity else float(max(1, math.ceil(budget / 2000)))
    B = int(math.floor(budget / g + 1e-9))
    inf = float("inf")
    dp = np.full(B + 1, inf)
    dp[0] = 0.0
    choice_tables: list[tuple[np.ndarray, list[int]]] = []
    for opts in groups:
        if not any(c <= 0 for c, _ in opts):
            raise ValueError("each group needs a zero-cost option")
        new = np.full(B + 1, inf)
        arg = np.full(B + 1, -1, dtype=int)
        units = []
        for i, (c, loss) in enumerate(opts):
            cu = int(math.ceil(max(0.0, c) / g - 1e-9))
            units.append(cu)
            if cu > B:
                continue
            cand = np.full(B + 1, inf)
            cand[cu:] = dp[: B + 1 - cu] + loss + 1e-7 * c
            better = cand < new
            new[better] = cand[better]
            arg[better] = i
        choice_tables.append((arg, units))
        dp = new
    b = int(np.argmin(dp))
    best = float(dp[b])
    picks: list[int] = []
    for arg, units in reversed(choice_tables):
        i = int(arg[b])
        picks.append(i)
        b -= units[i]
    picks.reverse()
    return picks, best


# ---------------------------------------------------------------- candidates

def position(ctx: SkuContext, sw: SkuWorld, horizon: int) -> float:
    inbound = sum(i.qty for i in sw.open_inbound if i.arrival_day is not None and i.arrival_day < horizon)
    return ctx.on_hand + inbound


def candidate_actions(ctx: SkuContext, sw: SkuWorld, rm: ReorderMath, defer_out: Outcome, budget: float) -> list[Action]:
    horizon = len(sw.demand_paths[0][1])
    shortfalls = []
    for _, path in sw.demand_paths:
        tmp = SkuWorld([(1.0, path)], sw.standard_arrival_day, sw.expedite_arrival_day, sw.open_inbound)
        shortfalls.append(evaluate(ctx, defer(ctx), tmp).units_short)
    need = rm.reorder_point + rm.mean_daily_demand * COVER_DAYS - rm.position
    raw = [need, 1.5 * need] + [s + rm.safety_stock for s in shortfalls if s > 0]
    top = max(raw, default=0.0)
    max_qty = budget / max(ctx.econ.unit_cost, 1e-9)
    raw += [f * min(top, max_qty) for f in PARTIAL_FRACTIONS]
    pack = max(1, int(ctx.case_pack))
    # Quantities above what the budget could ever buy are clamped (not dropped): when demand jumps, the
    # affordable maximum must stay on the menu or the SKU would silently fall back to "defer".
    cap = int(math.floor(max_qty / pack + 1e-9)) * pack
    cap = cap if cap >= ctx.moq else 0
    qtys = sorted({q if q <= max_qty else cap
                   for q in (round_order_qty(x, ctx.moq, ctx.case_pack) for x in raw)} - {0})

    po_opts: list[tuple[int, bool]] = []
    for q in qtys:
        if sw.standard_arrival_day is not None and sw.standard_arrival_day < horizon:
            po_opts.append((q, False))
        if sw.expedite_arrival_day is not None and sw.expedite_arrival_day < horizon and (
                sw.standard_arrival_day is None or sw.expedite_arrival_day < sw.standard_arrival_day):
            po_opts.append((q, True))

    mds: list[Markdown] = []
    lot_ids = {l.lot_id for l in ctx.lots}
    at_risk = {k: v for k, v in defer_out.expired_by_lot.items() if k in lot_ids and v > 0.5}
    if at_risk:
        lot_id = max(at_risk, key=at_risk.get)
        expiry = next(l.expiry_day for l in ctx.lots if l.lot_id == lot_id)
        starts = sorted({0, max(0, expiry - MARKDOWN_LATE_START_DAYS)})
        mds = [Markdown(lot_id, d, s) for s in starts for d in MARKDOWN_DISCOUNTS]

    acts: list[Action] = [defer(ctx)]
    base = dict(sku_id=ctx.sku_id, unit_cost=ctx.econ.unit_cost, expedite_premium_pct=ctx.expedite_premium_pct)
    for q, exp in po_opts:
        acts.append(Action(kind="expedited_po" if exp else "standard_po", order_qty=q, expedite=exp, **base))
    for md in mds:
        acts.append(Action(kind="markdown", markdown=md, **base))
        for q, exp in po_opts:
            acts.append(Action(kind="markdown+expedited_po" if exp else "markdown+standard_po",
                               order_qty=q, expedite=exp, markdown=md, **base))
    return acts


# ---------------------------------------------------------------- planner

@dataclass
class SkuDecision:
    ctx: SkuContext
    action: Action
    outcome: Outcome
    defer_outcome: Outcome
    reorder: ReorderMath
    n_candidates: int
    world_notes: list[str] = field(default_factory=list)
    event_ids: list[int] = field(default_factory=list)

    @property
    def loss_avoided(self) -> float:
        return self.defer_outcome.total_loss - self.outcome.total_loss


@dataclass
class PlanResult:
    decisions: list[SkuDecision]
    budget: float
    expected_loss_plan: float
    expected_loss_no_action: float

    @property
    def cash_used(self) -> float:
        return sum(d.action.cash_outlay for d in self.decisions)

    @property
    def actions(self) -> list[Action]:
        return [d.action for d in self.decisions]


def build_plan(ctxs: Sequence[SkuContext], world: World, budget: float, z: float) -> PlanResult:
    groups, cands, outs, meta = [], [], [], []
    for ctx in ctxs:
        sw = world.skus[ctx.sku_id]
        horizon = len(sw.demand_paths[0][1])
        p50 = next((p for w, p in sw.demand_paths if w >= 0.5), sw.demand_paths[0][1])
        lt = sw.standard_arrival_day if sw.standard_arrival_day is not None else ctx.metrics.effective_lead_time_days
        rm = reorder_math(list(p50), list(ctx.history), ctx.metrics, z, ctx.moq, ctx.case_pack,
                          position(ctx, sw, horizon), lead_time_override=lt)
        d_out = evaluate(ctx, defer(ctx), sw)
        acts = candidate_actions(ctx, sw, rm, d_out, budget)
        o = [d_out] + [evaluate(ctx, a, sw) for a in acts[1:]]
        keep = [0] + [i for i in range(1, len(acts)) if o[i].total_loss < d_out.total_loss - MIN_BENEFIT_INR]
        acts = [acts[i] for i in keep]
        o = [o[i] for i in keep]
        groups.append([(a.cash_outlay, oo.total_loss) for a, oo in zip(acts, o)])
        cands.append(acts)
        outs.append(o)
        meta.append((rm, d_out, sw))
    picks, _ = solve_mckp(groups, budget)
    decisions = []
    for ctx, i, acts, o, (rm, d_out, sw) in zip(ctxs, picks, cands, outs, meta):
        decisions.append(SkuDecision(ctx, acts[i], o[i], d_out, rm, len(acts), list(sw.notes), list(sw.event_ids)))
    return PlanResult(
        decisions=decisions,
        budget=budget,
        expected_loss_plan=sum(d.outcome.total_loss for d in decisions),
        expected_loss_no_action=sum(d.defer_outcome.total_loss for d in decisions),
    )


def action_reasons(dec: SkuDecision, as_of: date) -> list[str]:
    r: list[str] = []
    d = dec.defer_outcome
    if d.first_stockout_day is not None:
        r.append(f"Without action, stockout expected from {as_of + timedelta(days=d.first_stockout_day)} "
                 f"({d.units_short:.0f} units short)")
    for lot_id, q in d.expired_by_lot.items():
        if q > 0.5:
            r.append(f"Lot {lot_id}: {q:.0f} units expected to expire unsold")
    if dec.action.expedite:
        r.append("Standard lead time arrives too late; expedited freight pays for itself")
    r.extend(dec.world_notes)
    return r


# ---------------------------------------------------------------- baseline

BASELINE_SAFETY_DAYS = 7
BASELINE_COVER_DAYS = 30


def baseline_actions(ctxs: Sequence[SkuContext], budget: float) -> list[Action]:
    """Static reorder point on *stated* lead time and trailing 28-day average demand; ignores signals,
    never marks down. SKUs with the lowest days of cover order first until the budget is exhausted."""
    rows = []
    for ctx in ctxs:
        avg = float(np.mean(ctx.history[-28:])) if len(ctx.history) else 0.0
        pos = ctx.on_hand + sum(p.qty for p in ctx.open_pos)
        rop = avg * (ctx.stated_lead_time + BASELINE_SAFETY_DAYS)
        if avg <= 0 or pos > rop:
            continue
        qty = round_order_qty(rop + avg * BASELINE_COVER_DAYS - pos, ctx.moq, ctx.case_pack)
        rows.append((pos / avg, ctx, qty))
    rows.sort(key=lambda r: (r[0], r[1].sku_id))
    remaining = budget
    chosen: dict[str, Action] = {}
    for _, ctx, qty in rows:
        cost = qty * ctx.econ.unit_cost
        if cost > remaining:
            qty = int(remaining // (ctx.econ.unit_cost * ctx.case_pack)) * ctx.case_pack
            if qty < ctx.moq or qty <= 0:
                continue
            cost = qty * ctx.econ.unit_cost
        remaining -= cost
        chosen[ctx.sku_id] = Action(ctx.sku_id, "standard_po", ctx.econ.unit_cost, ctx.expedite_premium_pct,
                                    order_qty=qty)
    return [chosen.get(c.sku_id, defer(c)) for c in ctxs]

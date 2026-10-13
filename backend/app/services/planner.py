"""Candidate actions, exact multi-choice knapsack under a cash budget, and the rule-based baseline."""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from typing import Sequence

import numpy as np

from .simulator import Markdown
from .supply import ReorderMath, reorder_math, round_order_qty
from .world import Action, Outcome, SkuContext, SkuWorld, World, defer, evaluate

COVER_DAYS = 14
PARTIAL_FRACTIONS = (0.15, 0.3, 0.45, 0.6, 0.75, 0.9)
MARKDOWN_DISCOUNTS = (0.2, 0.35, 0.5)
MARKDOWN_LATE_START_DAYS = 7
MIN_BENEFIT_INR = 1.0
STAGGER_FIRST_SHARES = (0.35, 0.5, 0.65)
STAGGER_DAYS = (7, 14)  # aligned with the weekly review cadence


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


def candidate_actions(ctx: SkuContext, sw: SkuWorld, rm: ReorderMath, defer_out: Outcome,
                      budget: float | Sequence[float], stagger: bool = False) -> list[Action]:
    """Menu of actions for one SKU. `budget` may be several budgets (for the budget frontier); the quantity
    ladder is the union over them so every budget level has affordable partial buys. With `stagger`, split
    buys are added: part today, the rest as a standard PO placed 7 or 14 days later."""
    horizon = len(sw.demand_paths[0][1])
    budgets = [float(budget)] if isinstance(budget, (int, float)) else [float(b) for b in budget]
    shortfalls = []
    for _, path in sw.demand_paths:
        shortfalls.append(evaluate(ctx, defer(ctx), replace(sw, demand_paths=[(1.0, path)])).units_short)
    need = rm.reorder_point + rm.mean_daily_demand * COVER_DAYS - rm.position
    base_raw = [need, 1.5 * need] + [s + rm.safety_stock for s in shortfalls if s > 0]
    top = max(base_raw, default=0.0)
    pack = max(1, int(ctx.case_pack))
    qset: set[int] = set()
    for b in budgets:
        max_qty = b / max(ctx.econ.unit_cost, 1e-9)
        raw = base_raw + [f * min(top, max_qty) for f in PARTIAL_FRACTIONS]
        # Quantities above what the budget could ever buy are clamped (not dropped): when demand jumps, the
        # affordable maximum must stay on the menu or the SKU would silently fall back to "defer".
        cap = int(math.floor(max_qty / pack + 1e-9)) * pack
        cap = cap if cap >= ctx.moq else 0
        qset |= {q if q <= max_qty else cap for q in (round_order_qty(x, ctx.moq, ctx.case_pack) for x in raw)}
    qtys = sorted(qset - {0})

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
    if stagger:
        acts.extend(staggered_actions(ctx, sw, qtys, po_opts, horizon))
    return acts


def staggered_actions(ctx: SkuContext, sw: SkuWorld, qtys: Sequence[int], po_opts: Sequence[tuple[int, bool]],
                      horizon: int) -> list[Action]:
    """Split buys: tranche 1 today (standard or expedited), tranche 2 a standard PO placed on a later weekly
    review. Only splits where both tranches respect MOQ / case pack and tranche 2 can land inside the horizon."""
    if sw.standard_arrival_day is None:
        return []
    base = dict(sku_id=ctx.sku_id, unit_cost=ctx.econ.unit_cost, expedite_premium_pct=ctx.expedite_premium_pct)
    exp_ok = any(e for _, e in po_opts)
    std_ok = any(not e for _, e in po_opts)
    seen: set[tuple] = set()
    out: list[Action] = []
    for total in qtys:
        for share in STAGGER_FIRST_SHARES:
            q1 = round_order_qty(total * share, ctx.moq, ctx.case_pack)
            q2 = round_order_qty(total - q1, ctx.moq, ctx.case_pack)
            if q1 <= 0 or q2 <= 0 or q1 >= total:
                continue
            for day in STAGGER_DAYS:
                if sw.standard_arrival_day + day >= horizon:
                    continue
                for exp in (False, True):
                    if (exp and not exp_ok) or (not exp and not std_ok) or (q1, q2, day, exp) in seen:
                        continue
                    seen.add((q1, q2, day, exp))
                    out.append(Action(kind="staggered_po", order_qty=q1, expedite=exp, later_qty=q2, later_day=day,
                                      **base))
    return out


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
    def cash_today(self) -> float:
        return sum(d.action.cash_today for d in self.decisions)

    @property
    def actions(self) -> list[Action]:
        return [d.action for d in self.decisions]

    @property
    def service_penalty_plan(self) -> float:
        return sum(d.outcome.service_penalty for d in self.decisions)


@dataclass
class SkuMenu:
    ctx: SkuContext
    sw: SkuWorld
    reorder: ReorderMath
    actions: list[Action]  # actions[0] is always defer
    outcomes: list[Outcome]

    @property
    def defer_outcome(self) -> Outcome:
        return self.outcomes[0]


@dataclass
class Menu:
    skus: list[SkuMenu]


def _reorder(ctx: SkuContext, sw: SkuWorld, z: float) -> ReorderMath:
    horizon = len(sw.demand_paths[0][1])
    p50 = next((p for w, p in sw.demand_paths if w >= 0.5), sw.demand_paths[0][1])
    lt = sw.standard_arrival_day if sw.standard_arrival_day is not None else ctx.metrics.effective_lead_time_days
    return reorder_math(list(p50), list(ctx.history), ctx.metrics, z, ctx.moq, ctx.case_pack,
                        position(ctx, sw, horizon), lead_time_override=lt)


def prepare_menu(ctxs: Sequence[SkuContext], world: World, budgets: float | Sequence[float], z: float,
                 stagger: bool = False) -> Menu:
    """Generate and score every candidate action once; the menu can then be solved at any budget up to
    max(budgets) (budget frontier) without re-simulating."""
    out = []
    for ctx in ctxs:
        sw = world.skus[ctx.sku_id]
        rm = _reorder(ctx, sw, z)
        d_out = evaluate(ctx, defer(ctx), sw)
        acts = candidate_actions(ctx, sw, rm, d_out, budgets, stagger)
        o = [d_out] + [evaluate(ctx, a, sw) for a in acts[1:]]
        keep = [0] + [i for i in range(1, len(acts)) if o[i].objective < d_out.objective - MIN_BENEFIT_INR]
        out.append(SkuMenu(ctx, sw, rm, [acts[i] for i in keep], [o[i] for i in keep]))
    return Menu(out)


def score_actions(ctxs: Sequence[SkuContext], world: World, actions: Sequence[Action], z: float,
                  budget: float) -> PlanResult:
    """Score a given set of actions (e.g. operator overrides) exactly like an optimised plan."""
    by = {a.sku_id: a for a in actions}
    decisions = []
    for ctx in ctxs:
        sw = world.skus[ctx.sku_id]
        d_out = evaluate(ctx, defer(ctx), sw)
        a = by.get(ctx.sku_id) or defer(ctx)
        o = d_out if a.kind == "defer" else evaluate(ctx, a, sw)
        decisions.append(SkuDecision(ctx, a, o, d_out, _reorder(ctx, sw, z), 1, list(sw.notes), list(sw.event_ids)))
    return PlanResult(decisions, budget, sum(d.outcome.total_loss for d in decisions),
                      sum(d.defer_outcome.total_loss for d in decisions))


def solve_menu(menu: Menu, budget: float, stagger: bool = False, capital_rate: float = 0.0) -> PlanResult:
    """Exact optimum of the menu under `budget`. Objective = financial loss + service penalty (priority SKUs),
    plus, when `capital_rate` > 0, the cost of tying up cash (which is what makes a later tranche cheaper).
    Staggered buys on the menu are only eligible when `stagger` is set."""
    idx = [[i for i, a in enumerate(m.actions) if stagger or a.later_qty == 0] for m in menu.skus]
    groups = []
    for m, ii in zip(menu.skus, idx):
        h = len(m.sw.demand_paths[0][1])
        groups.append([(m.actions[i].cash_outlay,
                        m.outcomes[i].objective + (m.actions[i].capital_cost(h, capital_rate) if capital_rate else 0.0))
                       for i in ii])
    picks, _ = solve_mckp(groups, budget)
    decisions = [SkuDecision(m.ctx, m.actions[ii[i]], m.outcomes[ii[i]], m.defer_outcome, m.reorder, len(ii),
                             list(m.sw.notes), list(m.sw.event_ids)) for m, ii, i in zip(menu.skus, idx, picks)]
    return PlanResult(
        decisions=decisions,
        budget=budget,
        expected_loss_plan=sum(d.outcome.total_loss for d in decisions),
        expected_loss_no_action=sum(d.defer_outcome.total_loss for d in decisions),
    )


def build_plan(ctxs: Sequence[SkuContext], world: World, budget: float, z: float) -> PlanResult:
    return solve_menu(prepare_menu(ctxs, world, budget, z), budget)


def frontier(menu: Menu, budgets: Sequence[float]) -> list[PlanResult]:
    return [solve_menu(menu, b) for b in budgets]


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
    if dec.action.later_qty:
        r.append(f"Split buy: {dec.action.order_qty} units now, {dec.action.later_qty} more ordered on "
                 f"{as_of + timedelta(days=dec.action.later_day)} (re-confirmed at that weekly review); "
                 f"later stock is fresher and less cash is tied up")
    if dec.ctx.priority == "hero" and dec.action.order_qty:
        r.append(f"Priority SKU: stockouts also carry a service penalty of INR {dec.ctx.goodwill_per_unit:.0f}/unit "
                 f"(lost repeat business)")
    r.extend(dec.world_notes)
    return r


# ---------------------------------------------------------------- baselines

BASELINE_SAFETY_DAYS = 7
BASELINE_COVER_DAYS = 30
TUNING_GRID = tuple((s, c) for s in (0, 3, 7, 10, 14) for c in (7, 14, 21, 30, 45))


def baseline_actions(ctxs: Sequence[SkuContext], budget: float, safety_days: float = BASELINE_SAFETY_DAYS,
                     cover_days: float = BASELINE_COVER_DAYS) -> list[Action]:
    """Static reorder point on *stated* lead time and trailing 28-day average demand; ignores signals,
    never marks down. SKUs with the lowest days of cover order first until the budget is exhausted."""
    rows = []
    for ctx in ctxs:
        avg = float(np.mean(ctx.history[-28:])) if len(ctx.history) else 0.0
        pos = ctx.on_hand + sum(p.qty for p in ctx.open_pos)
        rop = avg * (ctx.stated_lead_time + safety_days)
        if avg <= 0 or pos > rop:
            continue
        qty = round_order_qty(rop + avg * cover_days - pos, ctx.moq, ctx.case_pack)
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


def tuned_baseline(ctxs: Sequence[SkuContext], world: World, budget: float) -> tuple[list[Action], dict]:
    """The same reorder-point rule with its two knobs (safety days, cover days) grid-searched to minimise
    expected loss in the *planning* world, i.e. the best rule an operator could pick with today's
    information. It is a stronger, fairer comparison than the textbook defaults."""
    best: tuple[float, float, float, list[Action]] | None = None
    for s, c in TUNING_GRID:
        acts = baseline_actions(ctxs, budget, s, c)
        loss = sum(evaluate(ctx, a, world.skus[ctx.sku_id]).total_loss for ctx, a in zip(ctxs, acts))
        if best is None or loss < best[0] - 1e-6:
            best = (loss, s, c, acts)
    assert best is not None
    return best[3], {"safety_days": best[1], "cover_days": best[2], "planning_world_loss": round(best[0], 2),
                     "grid": f"safety days {sorted({s for s, _ in TUNING_GRID})} x cover days "
                             f"{sorted({c for _, c in TUNING_GRID})}"}

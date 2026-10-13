import itertools
import random

import numpy as np
import pytest

from app.services import planner, state, value
from app.services.planner import baseline_actions, build_plan, solve_mckp
from app.services.world import Action, evaluate


def _brute(groups, budget):
    best = (float("inf"), None)
    for combo in itertools.product(*[range(len(g)) for g in groups]):
        cost = sum(groups[i][c][0] for i, c in enumerate(combo))
        if cost <= budget + 1e-9:
            loss = sum(groups[i][c][1] for i, c in enumerate(combo))
            if loss < best[0] - 1e-9:
                best = (loss, combo)
    return best


@pytest.mark.parametrize("seed", range(25))
def test_knapsack_matches_brute_force(seed):
    rng = random.Random(seed)
    groups = []
    for _ in range(rng.randint(2, 5)):
        opts = [(0.0, rng.uniform(50, 200))]
        for _ in range(rng.randint(1, 3)):
            opts.append((float(rng.randint(1, 40)), rng.uniform(0, 150)))
        groups.append(opts)
    budget = float(rng.randint(10, 80))
    picks, loss = solve_mckp(groups, budget, granularity=1.0)
    exp_loss, _ = _brute(groups, budget)
    assert sum(groups[i][p][0] for i, p in enumerate(picks)) <= budget
    assert sum(groups[i][p][1] for i, p in enumerate(picks)) == pytest.approx(exp_loss, abs=1e-4)
    assert loss == pytest.approx(exp_loss, abs=1e-3)


def test_knapsack_coarse_granularity_never_exceeds_budget():
    rng = random.Random(7)
    groups = [[(0.0, 100.0)] + [(rng.uniform(1, 9000), rng.uniform(0, 90)) for _ in range(4)] for _ in range(12)]
    budget = 20000.0
    picks, _ = solve_mckp(groups, budget)
    assert sum(groups[i][p][0] for i, p in enumerate(picks)) <= budget


def test_knapsack_requires_zero_cost_option():
    with pytest.raises(ValueError):
        solve_mckp([[(5.0, 1.0)]], 10)


@pytest.fixture()
def worlds(db):
    snap = state.load_snapshot(db)
    ctxs = state.build_contexts(snap)
    return snap, ctxs, state.planning_world(snap, ctxs), state.evaluation_world(snap, ctxs)


@pytest.mark.parametrize("budget", [0, 200_000, 1_000_000, 3_000_000])
def test_plan_respects_budget_and_is_never_worse_than_doing_nothing(worlds, budget):
    snap, ctxs, pw, _ = worlds
    res = build_plan(ctxs, pw, budget, 1.645)
    cash = sum(a.cash_outlay for a in res.actions)
    assert cash <= budget + 1e-6
    assert res.cash_used == pytest.approx(cash)
    assert res.expected_loss_plan <= res.expected_loss_no_action + 1e-6
    assert len(res.actions) == len(ctxs)  # one decision per SKU (defer allowed)
    for a in res.actions:
        ctx = next(c for c in ctxs if c.sku_id == a.sku_id)
        if a.order_qty:
            assert a.order_qty >= ctx.moq and a.order_qty % ctx.case_pack == 0


def test_more_budget_never_increases_planned_loss(worlds):
    _, ctxs, pw, _ = worlds
    losses = [build_plan(ctxs, pw, b, 1.645).expected_loss_plan for b in (0, 400_000, 1_000_000, 2_000_000)]
    assert all(b <= a + 1e-6 for a, b in zip(losses, losses[1:]))


def test_demand_surge_never_turns_a_buy_into_defer(worlds):
    """Regression: oversized order quantities used to be dropped, so a big demand jump left only 'defer'."""
    from app.services import scenarios

    _, ctxs, pw, _ = worlds
    budget = 400_000
    sku = next(d.ctx.sku_id for d in build_plan(ctxs, pw, budget, 1.645).decisions if d.action.order_qty > 0)
    surged = scenarios.apply(pw, ctxs, scenarios.ScenarioSpec(demand_multiplier=5.0, affected_skus=[sku]))
    # Only this SKU competes for the budget, so a defer could only come from missing (dropped) buy options.
    alone = [c for c in ctxs if c.sku_id == sku]
    dec = build_plan(alone, surged, budget, 1.645).decisions[0]
    assert dec.action.order_qty > 0
    assert dec.action.cash_outlay <= budget + 1e-6


def test_baseline_respects_budget_and_orders_lowest_cover_first(worlds):
    _, ctxs, _, _ = worlds
    base = baseline_actions(ctxs, 1_000_000)
    orders = [a for a in base if a.order_qty > 0]
    assert sum(a.cash_outlay for a in base) <= 1_000_000 + 1e-6
    assert orders, "seed story should trigger rule-based orders"
    assert all(a.markdown is None and not a.expedite for a in base)


def test_plan_beats_rule_baseline_in_evaluation_world(worlds):
    _, ctxs, pw, ew = worlds
    res = build_plan(ctxs, pw, 1_000_000, 1.645)
    card = value.compare(ctxs, res.actions, baseline_actions(ctxs, 1_000_000), ew)
    assert card["savings"] > 0
    assert card["plan"]["total_loss"] < card["baseline"]["total_loss"]
    # markdowns are used for the short-dated lots in the seed story
    kinds = {a.sku_id: a.kind for a in res.actions}
    assert "markdown" in kinds["SKU-013"]


def test_value_excludes_cash_and_sums_components(worlds):
    _, ctxs, _, ew = worlds
    ctx = next(c for c in ctxs if c.sku_id == "SKU-015")
    sw = ew.skus[ctx.sku_id]
    idle = evaluate(ctx, Action(ctx.sku_id, "defer", ctx.econ.unit_cost), sw)
    buy = evaluate(ctx, Action(ctx.sku_id, "standard_po", ctx.econ.unit_cost, order_qty=ctx.case_pack * 20), sw)
    assert buy.cash_outlay == pytest.approx(ctx.case_pack * 20 * ctx.econ.unit_cost)
    for o in (idle, buy):
        assert o.total_loss == pytest.approx(o.lost_margin + o.expiry_writeoff + o.markdown_cost
                                             + o.cannibalisation + o.expedite_premium)
    assert buy.lost_margin < idle.lost_margin


def test_expedite_premium_counted_once(worlds):
    _, ctxs, _, ew = worlds
    ctx = next(c for c in ctxs if c.sku_id == "SKU-007")
    a = Action(ctx.sku_id, "expedited_po", ctx.econ.unit_cost, ctx.expedite_premium_pct, order_qty=96, expedite=True)
    o = evaluate(ctx, a, ew.skus[ctx.sku_id])
    assert o.expedite_premium == pytest.approx(96 * ctx.econ.unit_cost * ctx.expedite_premium_pct)
    assert o.cash_outlay == pytest.approx(96 * ctx.econ.unit_cost + o.expedite_premium)
    totals = value.evaluate_actions([ctx], [a], ew)["totals"]
    assert totals["total_loss"] == pytest.approx(o.total_loss, abs=0.05)


def test_wape():
    assert value.wape([10, 10], [5, 15]) == pytest.approx(0.5)
    assert value.wape([0, 0], [1, 1]) is None


def test_band_paths_preserve_mid_and_spread_grows_slowly():
    p50 = np.full(30, 10.0)
    paths = state.band_paths(p50 * 0.5, p50, p50 * 1.5)
    lo, mid, hi = (p for _, p in paths)
    assert mid.sum() == pytest.approx(300)
    assert hi.sum() == pytest.approx(300 + 5 * np.sqrt(30))
    assert (lo >= 0).all() and lo.sum() < 300

"""Robustness machinery: supply cases, tuned baseline, frontier, Monte Carlo worlds, rolling re-plan, ablation."""

import numpy as np
import pytest

from app.config import get_settings
from app.services import rolling, state, value
from app.services.planner import (TUNING_GRID, baseline_actions, build_plan, frontier, prepare_menu, score_actions,
                                  tuned_baseline)


@pytest.fixture()
def world(db):
    snap = state.load_snapshot(db)
    ctxs = state.build_contexts(snap)
    return snap, ctxs, state.planning_world(snap, ctxs), state.evaluation_world(snap, ctxs)


def test_supply_cases_are_ordered_and_weighted(world):
    snap, ctxs, pw, _ = world
    for c in ctxs:
        cases = pw.skus[c.sku_id].supply_cases
        assert len(cases) == 3 and sum(x.weight for x in cases) == pytest.approx(1.0)
        days = [x.standard_arrival_day for x in cases]
        assert days == sorted(days) and days[0] >= c.stated_lead_time
        for x in cases:
            for i, o in zip(x.open_inbound, c.open_pos):
                assert i.arrival_day >= o.expected_day


def test_priorities_mark_top_revenue_skus_as_heroes(world):
    snap, ctxs, _, _ = world
    pri = state.sku_priorities(snap)
    heroes = [k for k, v in pri.items() if v["priority"] == "hero"]
    assert len(heroes) == round(len(pri) * get_settings().hero_share)
    assert min(pri[h]["revenue_28d"] for h in heroes) >= max(v["revenue_28d"] for k, v in pri.items() if k not in heroes)
    by = {c.sku_id: c for c in ctxs}
    h = by[heroes[0]]
    assert h.goodwill_per_unit == pytest.approx(get_settings().goodwill_hero_pct * h.econ.unit_price)


def test_tuned_baseline_is_best_of_grid_in_planning_world(world):
    _, ctxs, pw, _ = world
    acts, info = tuned_baseline(ctxs, pw, 50000)
    loss = lambda a: sum(value.evaluate_actions(ctxs, a, pw)["totals"]["total_loss"] for _ in [0])  # noqa: E731
    best = loss(acts)
    assert best == pytest.approx(info["planning_world_loss"], abs=0.05)
    assert best <= loss(baseline_actions(ctxs, 50000)) + 1e-6
    s, c = TUNING_GRID[len(TUNING_GRID) // 2]
    assert best <= loss(baseline_actions(ctxs, 50000, s, c)) + 1e-6


def test_frontier_is_monotone_and_score_actions_matches_plan(world):
    _, ctxs, pw, _ = world
    z = get_settings().service_level_z
    menu = prepare_menu(ctxs, pw, [0, 25000, 50000, 100000], z)
    res = frontier(menu, [0, 25000, 50000, 100000])
    obj = [r.expected_loss_plan + r.service_penalty_plan for r in res]
    assert all(a >= b - 1e-6 for a, b in zip(obj, obj[1:]))
    assert res[0].cash_used == 0
    plan = build_plan(ctxs, pw, 50000, z)
    again = score_actions(ctxs, pw, plan.actions, z, 50000)
    assert again.expected_loss_plan == pytest.approx(plan.expected_loss_plan, abs=0.01)


def test_monte_carlo_worlds_are_reproducible_and_reported(world):
    snap, ctxs, pw, _ = world
    w1 = state.monte_carlo_worlds(snap, ctxs, 5, seed=3)
    w2 = state.monte_carlo_worlds(snap, ctxs, 5, seed=3)
    sku = ctxs[0].sku_id
    assert all(np.array_equal(a.skus[sku].demand_paths[0][1], b.skus[sku].demand_paths[0][1]) for a, b in zip(w1, w2))
    plan = build_plan(ctxs, pw, 50000, get_settings().service_level_z).actions
    mc = value.monte_carlo(ctxs, {"plan": plan, "static": baseline_actions(ctxs, 50000)}, w1)
    sv = mc["savings"]["plan_vs_static"]
    assert mc["n_worlds"] == 5 and sv["p5"] <= sv["mean"] <= sv["p95"] and 0 <= sv["win_rate"] <= 1
    assert 0 <= mc["policies"]["plan"]["fill_rate"]["mean"] <= 1


def test_rolling_plan_once_equals_one_shot_evaluation(world):
    snap, ctxs, pw, ew = world
    plan = build_plan(ctxs, pw, 50000, get_settings().service_level_z).actions
    one = value.evaluate_actions(ctxs, plan, ew)["totals"]
    truth = rolling.truth_from_world(ew, ctxs)
    rolled = rolling.simulate_policy(snap, ctxs, truth, 50000, rolling.plan_once(plan))
    assert rolled["total_loss"] == pytest.approx(one["total_loss"], abs=0.05)
    assert rolled["units_short"] == pytest.approx(one["units_short"], abs=0.01)
    weekly = rolling.simulate_policy(snap, ctxs, truth, 50000, rolling.replan_weekly(snap, plan, 1.645))
    assert weekly["cash_spent"] <= 50000 + 1e-6


def test_without_signals_divides_out_confirmed_multipliers(world):
    snap, ctxs, _, _ = world
    snap2, ctxs2 = state.without_signals(snap, ctxs)
    assert snap2.confirmed == []
    assert all(a.forecast.p50 == b.forecast.p50 for a, b in zip(ctxs, ctxs2))  # no confirmed signals in the seed

"""Plan options: staggered buys (scheduled second tranche), reserve / lean strategies, shock worlds, and the
option comparison under weekly re-planning."""

import csv
import io

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app
from app.services import plans, rolling, state, value
from app.services.planner import STAGGER_DAYS, prepare_menu, solve_menu
from app.services.world import Action, defer, evaluate


@pytest.fixture()
def world(db):
    snap = state.load_snapshot(db)
    ctxs = state.build_contexts(snap)
    return snap, ctxs, state.planning_world(snap, ctxs), state.evaluation_world(snap, ctxs)


def _menu(ctxs, pw, budget=1_000_000):
    return prepare_menu(ctxs, pw, [budget], get_settings().service_level_z, stagger=True)


def test_staggered_candidates_respect_pack_and_horizon(world):
    snap, ctxs, pw, _ = world
    menu = _menu(ctxs, pw)
    by = {c.sku_id: c for c in ctxs}
    split = [a for m in menu.skus for a in m.actions if a.later_qty]
    assert split, "expected at least one staggered buy to beat deferring"
    for a in split:
        c = by[a.sku_id]
        assert a.kind == "staggered_po" and a.later_day in STAGGER_DAYS
        for q in (a.order_qty, a.later_qty):
            assert q >= c.moq and q % c.case_pack == 0
        assert pw.skus[a.sku_id].standard_arrival_day + a.later_day < snap.horizon
        assert a.cash_outlay == pytest.approx(a.cash_today + a.later_qty * c.econ.unit_cost)
        once = Action(a.sku_id, "standard_po", a.unit_cost, a.expedite_premium_pct,
                      order_qty=a.order_qty + a.later_qty, expedite=a.expedite)
        assert a.capital_cost(30, 0.18) < once.capital_cost(30, 0.18)
        assert Action.from_dict(a.to_dict(), a.unit_cost, a.expedite_premium_pct).key() == a.key()


def test_later_tranche_arrives_later_in_evaluation(world):
    _, ctxs, pw, _ = world
    c = ctxs[0]
    sw = pw.skus[c.sku_id]
    a = Action(c.sku_id, "staggered_po", c.econ.unit_cost, c.expedite_premium_pct, order_qty=c.moq,
               later_qty=c.moq, later_day=7)
    o = evaluate(c, a, sw)
    assert o.later_arrival_day == sw.standard_arrival_day + 7
    both_now = evaluate(c, Action(c.sku_id, "standard_po", c.econ.unit_cost, c.expedite_premium_pct,
                                  order_qty=2 * c.moq), sw)
    assert o.units_short >= both_now.units_short - 1e-9  # later stock can only help less with shortages


def test_solve_menu_stagger_flag(world):
    _, ctxs, pw, _ = world
    menu = _menu(ctxs, pw)
    rate, snap_h = get_settings().working_capital_rate, 30
    commit = solve_menu(menu, 1_000_000)
    stag = solve_menu(menu, 1_000_000, stagger=True, capital_rate=rate)
    assert not any(a.later_qty for a in commit.actions)
    assert stag.cash_used <= 1_000_000 + 1e-6 and stag.cash_today <= stag.cash_used

    def total(r):
        return r.expected_loss_plan + r.service_penalty_plan + sum(a.capital_cost(snap_h, rate) for a in r.actions)
    # commit-now's picks are feasible for the staggered problem (same cost rounding), so it cannot do worse
    assert total(stag) <= total(commit) + 0.01


def test_plan_core_builds_four_options(world):
    snap, ctxs, pw, ew = world
    s = get_settings()
    result, _, rows, summary = plans._plan_core(snap, ctxs, pw, ew, 1_000_000.0, strategy="staggered")
    opts = {o["key"]: o for o in summary["options"]}
    assert set(opts) == set(plans.STRATEGIES)
    assert opts["staggered"]["is_current"] and summary["strategy"] == "staggered"
    assert opts["reserve"]["cash_committed"] <= 1_000_000 * (1 - s.reserve_share) + 1e-6
    assert opts["lean"]["operating_budget"] <= 1_000_000
    assert opts["commit_now"]["cash_scheduled"] == 0
    for o in opts.values():
        assert o["cash_today"] + o["cash_scheduled"] == pytest.approx(o["cash_committed"], abs=0.05)
    assert summary["cash_today"] == pytest.approx(result.cash_today, abs=0.05)
    for r in rows:
        if r["later_qty"]:
            assert r["later_order_date"] > summary["as_of"] and r["later_arrival_date"]
    with pytest.raises(ValueError):
        plans._plan_core(snap, ctxs, pw, ew, 1_000_000.0, strategy="yolo")


def test_plan_once_follows_schedule_and_replan_releases_it(world):
    snap, ctxs, pw, ew = world
    stag = solve_menu(_menu(ctxs, pw), 1_000_000, stagger=True, capital_rate=0.18)
    if not any(a.later_qty for a in stag.actions):
        pytest.skip("no staggered buy chosen on this seed")
    truth = rolling.truth_from_world(ew, ctxs)
    once = rolling.simulate_policy(snap, ctxs, truth, 1_000_000, rolling.plan_once(stag.actions))
    one_shot = value.evaluate_actions(ctxs, stag.actions, ew)["totals"]
    assert once["total_loss"] == pytest.approx(one_shot["total_loss"], abs=0.05)
    assert once["cash_day0"] == pytest.approx(stag.cash_today, abs=0.05)
    rel = rolling.simulate_policy(snap, ctxs, truth, 1_000_000, rolling.replan_weekly(snap, stag.actions, 1.645),
                                  confirm=rolling.release_to_replan)
    assert rel["tranches_released"] == sum(1 for a in stag.actions if a.later_qty)
    assert rel["cash_spent"] <= 1_000_000 + 1e-6


def test_shock_worlds_are_reproducible_and_shift_demand(world):
    snap, ctxs, _, _ = world
    calm = state.monte_carlo_worlds(snap, ctxs, 3, seed=5)
    a, b = rolling.shock_worlds(calm, ctxs, seed=2), rolling.shock_worlds(calm, ctxs, seed=2)
    for wa, wb, wc in zip(a, b, calm):
        assert wa.description["shocked_skus"] == wb.description["shocked_skus"]
        for sh in wa.description["shocked_skus"]:
            p, base = wa.skus[sh["sku_id"]].demand_paths[0][1], wc.skus[sh["sku_id"]].demand_paths[0][1]
            assert np.array_equal(p[:sh["from_day"]], base[:sh["from_day"]])
        sup = wa.description["slipped_supplier"]
        c = next(c for c in ctxs if c.supplier_id == sup)
        assert wa.skus[c.sku_id].standard_arrival_day == wc.skus[c.sku_id].standard_arrival_day + \
            wa.description["slip_days"]


def test_compare_options_is_paired_and_picks_best(world):
    snap, ctxs, pw, ew = world
    menu = _menu(ctxs, pw)
    opts = {"commit_now": (solve_menu(menu, 1_000_000).actions, 1_000_000.0),
            "reserve": (solve_menu(menu, 700_000).actions, 1_000_000.0)}
    calm = state.monte_carlo_worlds(snap, ctxs, 1)
    res = rolling.compare_options(snap, ctxs, ew, calm, rolling.shock_worlds(calm, ctxs), opts, 1.645, 0.18)
    assert set(res["options"]) == {"commit_now", "reserve"}
    assert set(res["best"]) == {"actual", "calm", "shock"}
    r = res["options"]["reserve"]
    assert r["actual"]["cash_day0"] <= 700_000 + 1e-6 and r["actual"]["cash_spent"] <= 1_000_000 + 1e-6
    diff = res["options"]["commit_now"]["calm"]["total_loss"]["mean"] - r["calm"]["total_loss"]["mean"]
    assert r["vs_commit_now"]["calm"]["mean"] == pytest.approx(diff, abs=0.05)


def test_api_strategy_and_scheduled_export(seeded_db):
    with TestClient(app) as client:
        assert client.post("/api/plans", json={"budget": 1_000_000, "strategy": "nope"}).status_code == 422
        plan = client.post("/api/plans", json={"budget": 1_000_000, "strategy": "staggered"}).json()
        s = plan["summary"]
        assert s["strategy"] == "staggered" and len(s["options"]) == 4
        rob = client.get(f"/api/plans/{plan['id']}").json()["summary"]["robustness"]
        assert rob["status"] == "done" and set(rob["options"]["options"]) == set(plans.STRATEGIES)
        client.post(f"/api/plans/{plan['id']}/approve")
        po = list(csv.DictReader(io.StringIO(client.get(f"/api/plans/{plan['id']}/export/po").text)))
        later = [a for a in plan["actions"] if a["later_qty"]]
        assert sum(1 for r in po if r["status"].startswith("scheduled")) == len(later)
        assert len(po) == sum(1 for a in plan["actions"] if a["order_qty"]) + len(later)
        assert sum(float(r["line_total_inr"]) + float(r["expedite_premium_inr"]) for r in po) == \
            pytest.approx(s["cash_used"], abs=1)

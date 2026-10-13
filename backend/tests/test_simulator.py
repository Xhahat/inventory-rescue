import pytest

from app.services.simulator import MARKDOWN_ELASTICITY, Inbound, Lot, Markdown, ProductEconomics, simulate

ECON = ProductEconomics(unit_cost=60.0, unit_price=100.0, shelf_life_days=60)


def test_stockout_lost_margin_and_arrival():
    r = simulate(ECON, [Lot("A", 20, 100)], [Inbound("PO", 50, 5)], [10.0] * 10)
    # days 0-1 sell 20, days 2-4 short 30 units, arrival day 5 covers days 5-9 (50 units)
    assert r.unmet_units == pytest.approx(30)
    assert r.lost_margin == pytest.approx(30 * 40)
    assert r.stockout_days == 3 and r.first_stockout_day == 2
    assert r.ending_units == pytest.approx(0)


def test_fefo_sells_earliest_expiry_first_and_writes_off_expired():
    lots = [Lot("LATE", 50, 100), Lot("EARLY", 30, 3)]
    r = simulate(ECON, lots, [], [5.0] * 10)
    # EARLY sells 5/day on days 0-2 (15), remaining 15 expire at start of day 3
    assert r.expired_by_lot == {"EARLY": pytest.approx(15)}
    assert r.expiry_writeoff == pytest.approx(15 * 60)
    assert r.ending_units == pytest.approx(50 - 35)
    assert r.unmet_units == 0


def test_never_arriving_inbound_is_ignored():
    r = simulate(ECON, [], [Inbound("PO", 100, None), Inbound("LATE", 100, 99)], [1.0] * 5)
    assert r.unmet_units == pytest.approx(5)


def test_inbound_shelf_life_starts_at_arrival():
    econ = ProductEconomics(60, 100, shelf_life_days=3)
    r = simulate(econ, [], [Inbound("PO", 30, 2)], [1.0] * 10)
    # arrives day 2, expires day 5: sells days 2,3,4 then 27 expire
    assert r.expired_units == pytest.approx(27)


def test_markdown_reduces_expiry_and_books_costs_without_double_counting():
    lots = [Lot("SHORT", 100, 6)]
    demand = [5.0] * 10
    base = simulate(ECON, lots, [], demand)
    md = simulate(ECON, lots, [], demand, Markdown("SHORT", 0.5, 0))
    assert base.expired_units == pytest.approx(70)
    uplift = 1 + MARKDOWN_ELASTICITY * 0.5
    sold = min(100, 6 * 5 * uplift)
    assert md.expired_units == pytest.approx(100 - sold)
    assert md.markdown_units == pytest.approx(sold)
    # base units carry the full discount (50); extra clearance units only the below-cost part (60-50=10)
    base_units = 6 * 5
    assert md.markdown_cost == pytest.approx(base_units * 50 + (sold - base_units) * 10)
    assert md.expiry_writeoff + md.markdown_cost < base.expiry_writeoff
    # units are conserved: sold + expired + ending == start
    assert md.units_sold + md.expired_units + md.ending_units == pytest.approx(100)


def test_markdown_start_day_respected():
    lots = [Lot("SHORT", 100, 8)]
    r = simulate(ECON, lots, [], [5.0] * 10, Markdown("SHORT", 0.2, 5))
    late = simulate(ECON, lots, [], [5.0] * 10, Markdown("SHORT", 0.2, 0))
    assert r.markdown_units < late.markdown_units
    assert r.markdown_units == pytest.approx(3 * 5 * (1 + MARKDOWN_ELASTICITY * 0.2))

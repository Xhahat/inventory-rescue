"""30-day daily inventory simulator with FEFO lots, inbound arrivals and markdowns (pure)."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Sequence

EPS = 1e-9

# Concave markdown response: extra clearance demand = base * A * (1 - exp(-discount / s)).
# Deeper discounts help less and less (diminishing returns). A = saturation uplift, s = discount scale.
# These are assumptions per category; replace them with values measured from past markdowns.
CATEGORY_ELASTICITY: dict[str, tuple[float, float]] = {
    "Beverages": (1.8, 0.25),
    "Snacks": (1.6, 0.25),
    "Breakfast & Spreads": (1.3, 0.30),
    "Protein": (1.1, 0.30),
    "Supplements": (0.8, 0.30),
}
DEFAULT_ELASTICITY = (1.4, 0.28)


def markdown_uplift(discount_pct: float, category: str | None = None) -> float:
    """Extra demand (as a multiple of base demand) created by a markdown of `discount_pct` (0..1)."""
    a, s = CATEGORY_ELASTICITY.get(category or "", DEFAULT_ELASTICITY)
    return a * (1.0 - math.exp(-max(0.0, discount_pct) / s))


@dataclass(frozen=True)
class Lot:
    lot_id: str
    qty: float
    expiry_day: int  # day index relative to as-of; lot is written off at the start of this day


@dataclass(frozen=True)
class Inbound:
    ref_id: str
    qty: float
    arrival_day: int | None  # None = never arrives within the horizon


@dataclass(frozen=True)
class Markdown:
    lot_id: str
    discount_pct: float  # 0..1
    start_day: int = 0


@dataclass(frozen=True)
class ProductEconomics:
    unit_cost: float
    unit_price: float
    shelf_life_days: int

    @property
    def unit_margin(self) -> float:
        return self.unit_price - self.unit_cost


@dataclass
class SimResult:
    units_sold: float = 0.0
    demand_units: float = 0.0
    incremental_units: float = 0.0
    unmet_units: float = 0.0
    lost_margin: float = 0.0
    expired_units: float = 0.0
    expiry_writeoff: float = 0.0
    markdown_units: float = 0.0
    markdown_cost: float = 0.0
    revenue: float = 0.0
    ending_units: float = 0.0
    ending_value: float = 0.0
    stockout_days: int = 0
    first_stockout_day: int | None = None
    expired_by_lot: dict[str, float] = field(default_factory=dict)
    daily_stock: list[float] = field(default_factory=list)
    final_lots: list[Lot] = field(default_factory=list)  # stock left after the last day (for rolling re-plans)


def simulate(
    econ: ProductEconomics,
    lots: Sequence[Lot],
    inbound: Sequence[Inbound],
    demand: Sequence[float],
    markdown: Markdown | None = None,
    uplift: float | None = None,
) -> SimResult:
    """Simulate len(demand) days. Day order: expire -> arrivals -> sales (FEFO) -> record stock.

    Lost margin counts only unmet *base* demand. During a markdown, base-demand units sold from the marked lot
    carry the full discount as markdown cost (own-SKU cannibalisation). Extra clearance units (uplift, see
    `markdown_uplift`) are served only from the marked lot; they would otherwise not sell, so their markdown cost
    is only the amount sold below unit cost, max(0, cost - discounted price). Cross-SKU cannibalisation is
    priced by the caller from `incremental_units`.
    """
    if markdown is not None and uplift is None:
        uplift = markdown_uplift(markdown.discount_pct)
    stock: list[list] = [[l.lot_id, float(l.qty), int(l.expiry_day)] for l in lots if l.qty > EPS]
    res = SimResult()
    horizon = len(demand)
    arrivals: dict[int, list[Inbound]] = {}
    for ib in inbound:
        if ib.arrival_day is None or ib.qty <= EPS or ib.arrival_day >= horizon:
            continue
        arrivals.setdefault(max(0, int(ib.arrival_day)), []).append(ib)

    for day in range(horizon):
        kept = []
        for lot in stock:
            if lot[2] <= day:
                res.expired_units += lot[1]
                res.expired_by_lot[lot[0]] = res.expired_by_lot.get(lot[0], 0.0) + lot[1]
            else:
                kept.append(lot)
        stock = kept
        for ib in arrivals.get(day, []):
            stock.append([ib.ref_id, float(ib.qty), day + econ.shelf_life_days])
        stock.sort(key=lambda l: l[2])

        md_active = markdown is not None and day >= markdown.start_day
        base = max(0.0, float(demand[day]))
        res.demand_units += base
        remaining = base
        for lot in stock:
            if remaining <= EPS:
                break
            take = min(lot[1], remaining)
            if take <= 0:
                continue
            lot[1] -= take
            remaining -= take
            _book_sale(res, econ, take, md_active and lot[0] == markdown.lot_id, markdown)
        if md_active:
            extra = base * uplift
            for lot in stock:
                if lot[0] == markdown.lot_id and lot[1] > EPS and extra > EPS:
                    take = min(lot[1], extra)
                    lot[1] -= take
                    res.incremental_units += take
                    _book_sale(res, econ, take, True, markdown, incremental=True)
        stock = [l for l in stock if l[1] > EPS]
        if remaining > EPS:
            res.unmet_units += remaining
            res.stockout_days += 1
            if res.first_stockout_day is None:
                res.first_stockout_day = day
        res.daily_stock.append(round(sum(l[1] for l in stock), 3))

    res.lost_margin = res.unmet_units * econ.unit_margin
    res.expiry_writeoff = res.expired_units * econ.unit_cost
    res.ending_units = sum(l[1] for l in stock)
    res.ending_value = res.ending_units * econ.unit_cost
    res.final_lots = [Lot(l[0], l[1], l[2]) for l in stock]
    return res


def _book_sale(res: SimResult, econ: ProductEconomics, units: float, discounted: bool, md: Markdown | None,
               incremental: bool = False) -> None:
    res.units_sold += units
    if discounted and md is not None:
        price = econ.unit_price * (1 - md.discount_pct)
        res.markdown_units += units
        if incremental:
            res.markdown_cost += units * max(0.0, econ.unit_cost - price)
        else:
            res.markdown_cost += units * econ.unit_price * md.discount_pct
        res.revenue += units * price
    else:
        res.revenue += units * econ.unit_price

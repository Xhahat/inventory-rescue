"""Pure domain types shared by planner, baseline, value and alerts.

A *World* is a set of assumptions about the next N days: demand paths, and supply cases (when new and open POs
arrive). The planner optimises in a planning world (forecast bands x supplier delay distribution); the value
scorecard replays the plan and the baselines in evaluation worlds ("actual" demand and lead times, plus Monte
Carlo perturbations of them).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Callable, Iterable

import numpy as np

from .simulator import Inbound, Lot, Markdown, ProductEconomics, SimResult, markdown_uplift, simulate
from .supply import SupplierMetrics

# Share of markdown-driven extra units that would otherwise have been bought as a same-category substitute.
CANNIBALISATION_SHARE = 0.3


@dataclass(frozen=True)
class OpenPO:
    po_id: str
    qty: float
    expected_day: int
    supplier_id: str


@dataclass
class ForecastBand:
    p10: list[float]
    p50: list[float]
    p90: list[float]
    method: str = "statistical"
    forecast_id: int | None = None
    drivers: list[str] = field(default_factory=list)


@dataclass
class SkuContext:
    sku_id: str
    name: str
    category: str
    supplier_id: str
    econ: ProductEconomics
    moq: int
    case_pack: int
    stated_lead_time: int
    expedite_lead_time: int
    expedite_premium_pct: float
    lots: list[Lot]
    open_pos: list[OpenPO]
    history: np.ndarray
    metrics: SupplierMetrics
    forecast: ForecastBand
    priority: str = "standard"  # hero | standard
    goodwill_per_unit: float = 0.0  # service penalty per unit short (lost future business), planner-only
    substitute_margin: float = 0.0  # avg unit margin of other SKUs in the category (cannibalisation cost)

    @property
    def on_hand(self) -> float:
        return float(sum(l.qty for l in self.lots))


@dataclass(frozen=True)
class SupplyCase:
    """One lead-time outcome for the SKU's supplier: when a new standard PO lands and when open POs land."""
    weight: float
    standard_arrival_day: int | None
    open_inbound: tuple[Inbound, ...]
    label: str = ""


@dataclass
class SkuWorld:
    demand_paths: list[tuple[float, np.ndarray]]
    standard_arrival_day: int | None  # nominal (shown to users; used when there are no supply cases)
    expedite_arrival_day: int | None
    open_inbound: list[Inbound]
    notes: list[str] = field(default_factory=list)
    event_ids: list[int] = field(default_factory=list)
    supply_cases: list[SupplyCase] = field(default_factory=list)

    def cases(self) -> list[tuple[float, int | None, list[Inbound]]]:
        if self.supply_cases:
            return [(c.weight, c.standard_arrival_day, list(c.open_inbound)) for c in self.supply_cases]
        return [(1.0, self.standard_arrival_day, list(self.open_inbound))]

    def map_supply(self, std_fn: Callable[[int | None], int | None],
                   inbound_fn: Callable[[list[Inbound]], list[Inbound]], **changes) -> "SkuWorld":
        return replace(
            self,
            standard_arrival_day=std_fn(self.standard_arrival_day),
            open_inbound=inbound_fn(list(self.open_inbound)),
            supply_cases=[SupplyCase(c.weight, std_fn(c.standard_arrival_day), tuple(inbound_fn(list(c.open_inbound))),
                                     c.label) for c in self.supply_cases],
            **changes,
        )


@dataclass
class World:
    name: str
    skus: dict[str, SkuWorld]
    description: dict = field(default_factory=dict)


@dataclass
class Action:
    sku_id: str
    kind: str  # defer | standard_po | expedited_po | markdown | markdown+standard_po | markdown+expedited_po
    unit_cost: float
    expedite_premium_pct: float = 0.0
    order_qty: int = 0
    expedite: bool = False
    markdown: Markdown | None = None

    @property
    def expedite_premium(self) -> float:
        return self.order_qty * self.unit_cost * self.expedite_premium_pct if self.expedite else 0.0

    @property
    def cash_outlay(self) -> float:
        return self.order_qty * self.unit_cost + self.expedite_premium

    def key(self) -> tuple:
        md = None if self.markdown is None else (self.markdown.lot_id, self.markdown.discount_pct, self.markdown.start_day)
        return (self.kind, self.order_qty, self.expedite, md)

    def to_dict(self) -> dict:
        return {
            "sku_id": self.sku_id,
            "kind": self.kind,
            "order_qty": self.order_qty,
            "expedite": self.expedite,
            "markdown": None if self.markdown is None else {
                "lot_id": self.markdown.lot_id,
                "discount_pct": self.markdown.discount_pct,
                "start_day": self.markdown.start_day,
            },
            "cash_outlay": round(self.cash_outlay, 2),
            "expedite_premium": round(self.expedite_premium, 2),
        }

    @staticmethod
    def from_dict(d: dict, unit_cost: float, premium_pct: float) -> "Action":
        md = d.get("markdown")
        return Action(
            sku_id=d["sku_id"], kind=d["kind"], unit_cost=unit_cost, expedite_premium_pct=premium_pct,
            order_qty=int(d.get("order_qty", 0)), expedite=bool(d.get("expedite", False)),
            markdown=None if not md else Markdown(md["lot_id"], float(md["discount_pct"]), int(md.get("start_day", 0))),
        )


def defer(ctx: SkuContext) -> Action:
    return Action(ctx.sku_id, "defer", ctx.econ.unit_cost, ctx.expedite_premium_pct)


LOSS_FIELDS = ("lost_margin", "expiry_writeoff", "markdown_cost", "cannibalisation", "expedite_premium")
SUM_FIELDS = LOSS_FIELDS + ("cash_outlay", "units_short", "demand_units", "expired_units", "markdown_units",
                            "ending_units", "ending_value", "service_penalty")


@dataclass
class Outcome:
    lost_margin: float = 0.0
    expiry_writeoff: float = 0.0
    markdown_cost: float = 0.0
    cannibalisation: float = 0.0
    expedite_premium: float = 0.0
    cash_outlay: float = 0.0
    units_short: float = 0.0
    demand_units: float = 0.0
    expired_units: float = 0.0
    markdown_units: float = 0.0
    ending_units: float = 0.0
    ending_value: float = 0.0
    stockout_days: float = 0.0
    service_penalty: float = 0.0
    first_stockout_day: int | None = None
    arrival_day: int | None = None
    expired_by_lot: dict[str, float] = field(default_factory=dict)

    @property
    def total_loss(self) -> float:
        """Financial loss over the horizon. Cash spent on stock is not a loss (it is still inventory)."""
        return self.lost_margin + self.expiry_writeoff + self.markdown_cost + self.cannibalisation + self.expedite_premium

    @property
    def objective(self) -> float:
        """What the planner minimises: financial loss plus the service penalty for priority SKUs."""
        return self.total_loss + self.service_penalty

    @property
    def fill_rate(self) -> float | None:
        return None if self.demand_units <= 0 else max(0.0, 1.0 - self.units_short / self.demand_units)

    def to_dict(self) -> dict:
        d = {k: (round(v, 2) if isinstance(v, float) else v) for k, v in self.__dict__.items()}
        d["expired_by_lot"] = {k: round(v, 1) for k, v in self.expired_by_lot.items()}
        d["total_loss"] = round(self.total_loss, 2)
        fr = self.fill_rate
        d["fill_rate"] = None if fr is None else round(fr, 4)
        return d


def evaluate(ctx: SkuContext, action: Action, sw: SkuWorld) -> Outcome:
    """Probability-weighted outcome of one SKU action in a world (demand paths x supply cases)."""
    md = action.markdown
    uplift = markdown_uplift(md.discount_pct, ctx.category) if md is not None else None
    nominal = None
    if action.order_qty > 0:
        nominal = sw.expedite_arrival_day if action.expedite else sw.standard_arrival_day
    out = Outcome(arrival_day=nominal)
    cases = sw.cases()
    total_w = (sum(w for w, _ in sw.demand_paths) or 1.0) * (sum(w for w, _, _ in cases) or 1.0)
    first_days: list[tuple[float, int]] = []
    incremental = 0.0
    for cw, std, open_inbound in cases:
        inbound = open_inbound
        if action.order_qty > 0:
            arrival = sw.expedite_arrival_day if action.expedite else std
            inbound = open_inbound + [Inbound(f"NEW-{ctx.sku_id}", float(action.order_qty), arrival)]
        for w, path in sw.demand_paths:
            r: SimResult = simulate(ctx.econ, ctx.lots, inbound, path, md, uplift)
            f = w * cw / total_w
            out.lost_margin += f * r.lost_margin
            out.expiry_writeoff += f * r.expiry_writeoff
            out.markdown_cost += f * r.markdown_cost
            out.units_short += f * r.unmet_units
            out.demand_units += f * r.demand_units
            out.expired_units += f * r.expired_units
            out.markdown_units += f * r.markdown_units
            out.ending_units += f * r.ending_units
            out.ending_value += f * r.ending_value
            out.stockout_days += f * r.stockout_days
            incremental += f * r.incremental_units
            for lot_id, q in r.expired_by_lot.items():
                out.expired_by_lot[lot_id] = out.expired_by_lot.get(lot_id, 0.0) + f * q
            if r.first_stockout_day is not None:
                first_days.append((f, r.first_stockout_day))
    if first_days and sum(f for f, _ in first_days) >= 0.5 - 1e-9:
        out.first_stockout_day = min(d for _, d in first_days)
    out.cannibalisation = CANNIBALISATION_SHARE * incremental * ctx.substitute_margin
    out.service_penalty = out.units_short * ctx.goodwill_per_unit
    out.expedite_premium = action.expedite_premium
    out.cash_outlay = action.cash_outlay
    return out


def shift_inbound(inbound: list[Inbound], days: int) -> list[Inbound]:
    return [Inbound(i.ref_id, i.qty, None if i.arrival_day is None else i.arrival_day + days) for i in inbound]


def transform_world(world: World, name: str, demand_mult: float = 1.0, extra_delay: int = 0) -> World:
    """Sensitivity helper: scale demand and/or delay every inbound arrival (new and open POs)."""
    def shift(d: int | None) -> int | None:
        return None if d is None else d + extra_delay

    skus = {}
    for sku_id, sw in world.skus.items():
        skus[sku_id] = sw.map_supply(
            shift, lambda ib: shift_inbound(ib, extra_delay),
            demand_paths=[(w, p * demand_mult) for w, p in sw.demand_paths],
            expedite_arrival_day=shift(sw.expedite_arrival_day),
        )
    return World(name, skus, {**world.description, "demand_mult": demand_mult, "extra_delay_days": extra_delay})


def sum_outcomes(outcomes: Iterable[Outcome]) -> dict:
    tot = Outcome()
    sku_days = 0.0
    for o in outcomes:
        for k in SUM_FIELDS:
            setattr(tot, k, getattr(tot, k) + getattr(o, k))
        sku_days += o.stockout_days
    d = {k: round(getattr(tot, k), 2) for k in SUM_FIELDS}
    d["total_loss"] = round(tot.total_loss, 2)
    d["stockout_sku_days"] = round(sku_days, 1)
    fr = tot.fill_rate
    d["fill_rate"] = None if fr is None else round(fr, 4)
    return d

"""Supplier metrics, lead times, safety stock and reorder points (pure functions)."""

from __future__ import annotations

import math
from dataclasses import dataclass, asdict
from datetime import date
from typing import Iterable, Sequence

import numpy as np


@dataclass(frozen=True)
class ReceiptRecord:
    po_date: date
    expected_date: date
    received_date: date
    ordered_qty: int
    received_qty: int


@dataclass(frozen=True)
class SupplierMetrics:
    supplier_id: str
    n_receipts: int
    on_time_rate: float
    fill_rate: float
    p80_delay_days: float
    mean_delay_days: float
    lead_time_mean_days: float
    lead_time_std_days: float
    stated_lead_time_days: int
    effective_lead_time_days: int

    def to_dict(self) -> dict:
        return asdict(self)


def supplier_metrics(supplier_id: str, stated_lead_time: int, receipts: Iterable[ReceiptRecord]) -> SupplierMetrics:
    rs = list(receipts)
    if not rs:
        return SupplierMetrics(supplier_id, 0, 1.0, 1.0, 0.0, 0.0, float(stated_lead_time), 0.0,
                               stated_lead_time, stated_lead_time)
    delays = np.array([max(0, (r.received_date - r.expected_date).days) for r in rs], dtype=float)
    lts = np.array([(r.received_date - r.po_date).days for r in rs], dtype=float)
    on_time = float(np.mean([r.received_date <= r.expected_date for r in rs]))
    ordered = sum(r.ordered_qty for r in rs)
    fill = float(sum(r.received_qty for r in rs) / ordered) if ordered else 1.0
    p80 = float(np.percentile(delays, 80))
    return SupplierMetrics(
        supplier_id=supplier_id,
        n_receipts=len(rs),
        on_time_rate=round(on_time, 4),
        fill_rate=round(min(fill, 1.0), 4),
        p80_delay_days=round(p80, 2),
        mean_delay_days=round(float(delays.mean()), 2),
        lead_time_mean_days=round(float(lts.mean()), 2),
        lead_time_std_days=round(float(lts.std(ddof=1)) if len(lts) > 1 else 0.0, 2),
        stated_lead_time_days=stated_lead_time,
        effective_lead_time_days=effective_lead_time(stated_lead_time, p80),
    )


def effective_lead_time(stated_lead_time: int, p80_delay: float) -> int:
    """Effective lead time = stated + P80 delay (rounded up to whole days)."""
    return int(stated_lead_time + math.ceil(p80_delay - 1e-9))


def safety_stock(z: float, lead_time: float, sigma_d: float, mean_d: float, sigma_lt: float) -> float:
    """SS = z * sqrt(LT * sigma_d^2 + d^2 * sigma_LT^2)."""
    return float(z * math.sqrt(max(0.0, lead_time * sigma_d ** 2 + mean_d ** 2 * sigma_lt ** 2)))


def reorder_point(mean_d: float, lead_time: float, ss: float) -> float:
    return float(mean_d * lead_time + ss)


def round_order_qty(qty: float, moq: int, case_pack: int) -> int:
    """Round a desired quantity up to a whole number of case packs, at least MOQ. 0 stays 0."""
    if qty <= 0:
        return 0
    case_pack = max(1, int(case_pack))
    q = max(float(qty), float(moq))
    return int(math.ceil(q / case_pack - 1e-9) * case_pack)


@dataclass(frozen=True)
class ReorderMath:
    mean_daily_demand: float
    sigma_daily_demand: float
    lead_time_days: int
    sigma_lead_time_days: float
    z: float
    safety_stock: float
    reorder_point: float
    moq: int
    case_pack: int
    position: float

    def to_dict(self) -> dict:
        return {k: (round(v, 2) if isinstance(v, float) else v) for k, v in asdict(self).items()}


def reorder_math(
    forecast_p50: Sequence[float],
    history: Sequence[float],
    metrics: SupplierMetrics,
    z: float,
    moq: int,
    case_pack: int,
    position: float,
    lead_time_override: int | None = None,
) -> ReorderMath:
    d = float(np.mean(forecast_p50)) if len(forecast_p50) else float(np.mean(history[-28:]) if len(history) else 0.0)
    recent = np.asarray(history[-28:], dtype=float)
    sigma_d = float(recent.std(ddof=1)) if len(recent) > 1 else 0.0
    lt = lead_time_override if lead_time_override is not None else metrics.effective_lead_time_days
    ss = safety_stock(z, lt, sigma_d, d, metrics.lead_time_std_days)
    return ReorderMath(d, sigma_d, int(lt), metrics.lead_time_std_days, z, ss, reorder_point(d, lt, ss),
                       moq, case_pack, float(position))

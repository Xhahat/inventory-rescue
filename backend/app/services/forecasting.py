"""Demand forecasting: LLM primary (validated) with a labelled seasonal-naive fallback; cached by input hash."""

from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Iterable, Sequence

import numpy as np

from ..llm import prompts
from ..llm.client import CallRecord, LLMClient, input_hash, run_json_task

Z90 = 1.2816
DEFAULT_EVENT_DAYS = 14
BOUND_FACTOR = 4.0
DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


@dataclass
class EventLike:
    """Minimal view of a confirmed signal or a context row used for adjustments."""
    id: Any
    kind: str
    start: date
    end: date
    multiplier: float
    description: str


@dataclass
class ForecastOutput:
    sku_id: str
    p10: list[float]
    p50: list[float]
    p90: list[float]
    drivers: list[str]
    method: str  # llm | fallback
    fallback_reason: str | None
    input_hash: str
    records: list[CallRecord] = field(default_factory=list)


# ---------------------------------------------------------------- pure helpers

def seasonal_naive(history: Sequence[float], horizon: int, weeks: int = 4) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    h = np.asarray(history, dtype=float)
    n = len(h)
    p50 = np.zeros(horizon)
    if n == 0:
        return p50, p50.copy(), p50.copy()
    for d in range(horizon):
        k0 = d // 7 + 1
        idx = [n + d - 7 * k for k in range(k0, k0 + weeks) if 0 <= n + d - 7 * k < n]
        p50[d] = float(np.mean(h[idx])) if idx else float(np.mean(h[-7:]))
    recent = h[-28:]
    if len(recent) >= 14:
        fitted = np.array([np.mean(recent[i % 7::7]) for i in range(len(recent))])
        sigma = float(np.std(recent - fitted, ddof=1))
    else:
        sigma = float(np.std(recent)) if len(recent) > 1 else 0.0
    p10 = np.maximum(0.0, p50 - Z90 * sigma)
    p90 = p50 + Z90 * sigma
    return p10, p50, p90


def multipliers(as_of: date, horizon: int, events: Iterable[EventLike]) -> tuple[np.ndarray, list[str]]:
    m = np.ones(horizon)
    drivers = []
    for ev in events:
        s = max(0, (ev.start - as_of).days)
        e = min(horizon - 1, (ev.end - as_of).days)
        if e < s or ev.multiplier <= 0:
            continue
        m[s:e + 1] *= ev.multiplier
        drivers.append(f"{ev.description} (x{ev.multiplier:g}, {ev.start}..{ev.end})")
    return m, drivers


def fallback_forecast(history: Sequence[float], as_of: date, horizon: int, events: Sequence[EventLike]):
    p10, p50, p90 = seasonal_naive(history, horizon)
    m, drivers = multipliers(as_of, horizon, events)
    drivers = ["Seasonal-naive: same weekday average of the last 4 weeks"] + drivers
    return (np.round(p10 * m, 2).tolist(), np.round(p50 * m, 2).tolist(), np.round(p90 * m, 2).tolist(), drivers)


EVENT_CHECK_MIN_MULT = 1.5


def _check_events_reflected(p50: Sequence[float], history: Sequence[float], mult: Sequence[float]) -> None:
    """Large confirmed events must visibly move p50 in their window: the lift over seasonal-naive has to be at
    least the square root of the event multiplier (half the effect on a log scale), in the right direction."""
    if not len(history):
        return
    mult = np.asarray(mult, dtype=float)
    base = seasonal_naive(history, len(mult))[1]
    p50 = np.asarray(p50, dtype=float)
    for v in np.unique(mult):
        if 1 / EVENT_CHECK_MIN_MULT < v < EVENT_CHECK_MIN_MULT:
            continue
        days = np.flatnonzero(mult == v)
        b = float(base[days].sum())
        if b <= 0:
            continue
        ratio = float(p50[days].sum()) / b
        need = math.sqrt(v)
        if (v > 1 and ratio < need) or (v < 1 and ratio > need):
            raise ValueError(f"forecast ignores a confirmed event: x{v:g} on days {days[0]}-{days[-1]} but p50 moves "
                             f"x{ratio:.2f} vs seasonal-naive (needs {'>=' if v > 1 else '<='} x{need:.2f})")


def validate_forecast(obj: dict, horizon: int, history: Sequence[float], mult: Sequence[float]) -> dict:
    out = {}
    for key in ("p10", "p50", "p90"):
        arr = obj.get(key)
        if not isinstance(arr, list) or len(arr) != horizon:
            raise ValueError(f"{key} must be a list of length {horizon}")
        vals = []
        for v in arr:
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(float(v)):
                raise ValueError(f"{key} contains a non-numeric value")
            if v < 0:
                raise ValueError(f"{key} contains a negative value")
            vals.append(float(v))
        out[key] = vals
    for d in range(horizon):
        if not (out["p10"][d] <= out["p50"][d] + 1e-6 and out["p50"][d] <= out["p90"][d] + 1e-6):
            raise ValueError(f"quantiles not ordered on day {d}")
    hist_max = max(1.0, float(np.max(history[-56:])) if len(history) else 1.0)
    for d in range(horizon):
        bound = BOUND_FACTOR * hist_max * max(1.0, float(mult[d]))
        if out["p90"][d] > bound:
            raise ValueError(f"day {d} p90 {out['p90'][d]:.1f} exceeds bound {bound:.1f} without a justifying event")
    _check_events_reflected(out["p50"], history, mult)
    drivers = obj.get("drivers", [])
    if not isinstance(drivers, list):
        raise ValueError("drivers must be a list")
    out["drivers"] = [str(x)[:200] for x in drivers[:5]]
    return out


def build_summary(sku: dict, history: Sequence[float], as_of: date, horizon: int,
                  confirmed: Sequence[EventLike], context_rows: Sequence[dict]) -> dict:
    h = np.asarray(history[-56:], dtype=float)
    start = as_of - timedelta(days=len(h))
    dow = {}
    for i, v in enumerate(h):
        dow.setdefault(DOW[(start + timedelta(days=i)).weekday()], []).append(v)
    return {
        "sku": sku,
        "as_of": as_of.isoformat(),
        "horizon_days": horizon,
        "forecast_start_weekday": DOW[as_of.weekday()],
        "history_start": start.isoformat(),
        "history_daily_units": [round(float(x), 1) for x in h],
        "stats": {
            "mean_28d": round(float(h[-28:].mean()), 2) if len(h) else 0,
            "mean_prev_28d": round(float(h[:-28].mean()), 2) if len(h) > 28 else None,
            "max_56d": round(float(h.max()), 1) if len(h) else 0,
            "std_28d": round(float(h[-28:].std()), 2) if len(h) else 0,
            "dow_mean": {k: round(float(np.mean(v)), 2) for k, v in dow.items()},
        },
        "confirmed_events": [
            {"id": f"EV-{e.id}", "type": e.kind, "start": e.start.isoformat(), "end": e.end.isoformat(),
             "multiplier": e.multiplier, "note": e.description} for e in confirmed
        ],
        "context": list(context_rows),
    }


# ---------------------------------------------------------------- orchestration

def forecast_one(llm: LLMClient | None, sku_id: str, summary: dict, history: Sequence[float], as_of: date,
                 horizon: int, events: Sequence[EventLike], h: str) -> ForecastOutput:
    mult, _ = multipliers(as_of, horizon, events)
    res = run_json_task(
        llm, "forecast", prompts.forecast_system(horizon), prompts.forecast_user(summary),
        lambda o: validate_forecast(o, horizon, history, mult), hash_parts=(prompts.PROMPT_VERSION,),
    )
    if res.ok and res.value:
        v = res.value
        return ForecastOutput(sku_id, v["p10"], v["p50"], v["p90"], v["drivers"], "llm", None, h, [res.record])
    p10, p50, p90, drivers = fallback_forecast(history, as_of, horizon, events)
    return ForecastOutput(sku_id, p10, p50, p90, drivers, "fallback", res.error, h, [res.record])


def forecast_many(llm: LLMClient | None, jobs: list[dict], max_workers: int) -> list[ForecastOutput]:
    """jobs: dicts with keys sku_id, summary, history, as_of, horizon, events, hash."""
    def run(j):
        return forecast_one(llm, j["sku_id"], j["summary"], j["history"], j["as_of"], j["horizon"], j["events"], j["hash"])

    if llm is None or len(jobs) <= 1:
        return [run(j) for j in jobs]
    with ThreadPoolExecutor(max_workers=max(1, max_workers)) as ex:
        return list(ex.map(run, jobs))


def forecast_hash(summary: dict, model: str) -> str:
    return input_hash("forecast", prompts.PROMPT_VERSION, model, summary)

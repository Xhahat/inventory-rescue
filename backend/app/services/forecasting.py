"""Hybrid demand forecasting.

1. Statistical base (always computed): context-cleaned history -> Holt-Winters (damped trend, weekly
   multiplicative seasonality; small parameter grid) or Croston/SBA for intermittent SKUs. p10/p90 come from
   empirical forecast errors at rolling in-sample origins, by horizon week.
2. Known horizon events (weather, holidays, competitor moves, promotions) and confirmed signals are applied as
   multipliers. Priors come from the context feed; confirmed signals are fixed by the operator.
3. The LLM may only *retune* the prior multiplier of each known context event (bounded) and add at most two small
   extra adjustments. It never writes the series itself, so a bad answer can only move demand within tight bounds.
   Invalid or missing LLM output -> the statistical forecast, labelled as such.
"""

from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Iterable, Sequence

import numpy as np

from ..llm import prompts
from ..llm.client import CallRecord, LLMClient, input_hash, run_json_task

STAT_VERSION = "hw-sba-1"
Z90 = 1.2816
DEFAULT_EVENT_DAYS = 14
DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
FIT_DAYS = 112
INTERMITTENT_ZERO_SHARE = 0.3
# LLM bounds
EVENT_MULT_RANGE = (0.3, 3.0)
EVENT_PRIOR_RATIO = (0.5, 2.0)
MAX_EXTRA = 2
EXTRA_MULT_RANGE = (0.8, 1.25)
MAX_DRIVERS = 5


@dataclass
class EventLike:
    """A demand driver over a date range. source: 'signal' (operator-confirmed, fixed) | 'context' (feed prior)."""
    id: Any
    kind: str
    start: date
    end: date
    multiplier: float
    description: str
    source: str = "signal"


@dataclass
class ForecastOutput:
    sku_id: str
    p10: list[float]
    p50: list[float]
    p90: list[float]
    drivers: list[str]
    method: str  # hybrid | statistical
    fallback_reason: str | None
    input_hash: str
    records: list[CallRecord] = field(default_factory=list)


@dataclass
class StatForecast:
    base_p10: np.ndarray  # before horizon events
    base_p50: np.ndarray
    base_p90: np.ndarray
    model: str
    detail: str


# ---------------------------------------------------------------- naive reference

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
    return np.maximum(0.0, p50 - Z90 * sigma), p50, p50 + Z90 * sigma


def multipliers(origin: date, n: int, events: Iterable[EventLike]) -> tuple[np.ndarray, list[str]]:
    """Product of event multipliers for days origin .. origin+n-1."""
    m = np.ones(n)
    drivers = []
    for ev in events:
        s = max(0, (ev.start - origin).days)
        e = min(n - 1, (ev.end - origin).days)
        if e < s or ev.multiplier <= 0:
            continue
        m[s:e + 1] *= ev.multiplier
        drivers.append(f"{ev.description} (x{ev.multiplier:.3g}, {ev.start}..{ev.end})")
    return m, drivers


# ---------------------------------------------------------------- Holt-Winters (vectorised over a grid)

_A, _B, _G, _P = np.meshgrid([0.05, 0.1, 0.2, 0.35], [0.0, 0.05, 0.15], [0.05, 0.15, 0.3], [0.85, 0.95],
                             indexing="ij")
GRID = np.stack([_A.ravel(), _B.ravel(), _G.ravel(), _P.ravel()], axis=1)  # (72, 4)
WARMUP = 14


def _hw_run(y: np.ndarray, params: np.ndarray, keep_states: bool = False):
    """Damped additive trend, multiplicative weekly season. params: (k, 4). Returns SSE per combo and,
    optionally, (level, trend, season) after each observation for the first combo."""
    k = params.shape[0]
    a, b, g, phi = (params[:, i] for i in range(4))
    n = len(y)
    lvl0 = max(float(y[:14].mean()), 1e-3)
    level = np.full(k, lvl0)
    trend = np.full(k, (float(y[7:14].mean()) - float(y[:7].mean())) / 7.0 if n >= 14 else 0.0)
    season = np.tile(np.array([max(0.05, float(y[i:14:7].mean()) / lvl0) if n >= 14 else 1.0 for i in range(7)]), (k, 1))
    sse = np.zeros(k)
    states = []
    for t in range(n):
        j = t % 7
        s = season[:, j]
        pred = np.maximum(0.0, (level + phi * trend) * s)
        if t >= WARMUP:
            sse += (y[t] - pred) ** 2
        new_level = np.maximum(1e-3, a * y[t] / s + (1 - a) * (level + phi * trend))
        trend = b * (new_level - level) + (1 - b) * phi * trend
        season[:, j] = np.maximum(0.05, g * y[t] / new_level + (1 - g) * s)
        level = new_level
        if keep_states:
            states.append((float(level[0]), float(trend[0]), season[0].copy()))
    return sse, states


def _hw_forecast(state, phi: float, t_next: int, h: int) -> np.ndarray:
    level, trend, season = state
    damp = np.cumsum(phi ** np.arange(1, h + 1))
    return np.maximum(0.0, (level + damp * trend) * season[(t_next + np.arange(h)) % 7])


def _croston_run(y: np.ndarray, alpha: float = 0.1):
    """SBA (Syntetos-Boylan) variant of Croston: states after each observation."""
    nz = np.flatnonzero(y > 0)
    z = float(y[nz].mean()) if len(nz) else 0.0
    p = float(len(y) / max(1, len(nz)))
    q = 1
    states = []
    for t in range(len(y)):
        if y[t] > 0:
            z = alpha * y[t] + (1 - alpha) * z
            p = alpha * q + (1 - alpha) * p
            q = 1
        else:
            q += 1
        states.append((1 - alpha / 2) * z / max(p, 1.0))
    return states


def _interval_quantiles(y: np.ndarray, fc_from, horizon: int) -> tuple[np.ndarray, np.ndarray]:
    """Empirical p10/p90 of log forecast ratios at rolling in-sample origins, by horizon week."""
    n = len(y)
    buckets: dict[int, list[float]] = {}
    for t0 in range(28, n - 1, 7):
        h = min(horizon, n - t0)
        f = fc_from(t0, h)
        r = np.log((y[t0:t0 + h] + 0.5) / (f + 0.5))
        for i, v in enumerate(r):
            buckets.setdefault(min(i // 7, 4), []).append(float(v))
    pooled = [v for vs in buckets.values() for v in vs]
    q10 = np.zeros(horizon)
    q90 = np.zeros(horizon)
    for d in range(horizon):
        vs = buckets.get(min(d // 7, 4), [])
        src = vs if len(vs) >= 20 else pooled
        if len(src) >= 5:
            q10[d] = min(0.0, float(np.percentile(src, 10)))
            q90[d] = max(0.0, float(np.percentile(src, 90)))
        else:
            q10[d], q90[d] = -0.4, 0.4
    return q10, q90


def statistical_base(clean: Sequence[float], horizon: int) -> StatForecast:
    y = np.asarray(clean, dtype=float)[-FIT_DAYS:]
    n = len(y)
    if n < 21 or float(y.sum()) <= 0:
        p10, p50, p90 = seasonal_naive(y, horizon)
        return StatForecast(p10, p50, p90, "seasonal_naive", "short history: same-weekday average")
    if float(np.mean(y == 0)) > INTERMITTENT_ZERO_SHARE:
        states = _croston_run(y)

        def fc_from(t0: int, h: int) -> np.ndarray:
            return np.full(h, states[t0 - 1])
        p50 = np.full(horizon, states[-1])
        model, detail = "croston_sba", f"Croston/SBA (intermittent: {np.mean(y == 0):.0%} zero days)"
    else:
        sse, _ = _hw_run(y, GRID)
        best = GRID[int(np.argmin(sse))]
        _, states = _hw_run(y, best[None, :], keep_states=True)
        phi = float(best[3])

        def fc_from(t0: int, h: int) -> np.ndarray:
            return _hw_forecast(states[t0 - 1], phi, t0, h)
        p50 = _hw_forecast(states[-1], phi, n, horizon)
        model = "holt_winters"
        detail = (f"Holt-Winters damped trend, weekly seasonality (alpha={best[0]:g}, beta={best[1]:g}, "
                  f"gamma={best[2]:g}, phi={best[3]:g})")
    q10, q90 = _interval_quantiles(y, fc_from, horizon)
    p10 = np.maximum(0.0, (p50 + 0.5) * np.exp(q10) - 0.5)
    p90 = (p50 + 0.5) * np.exp(q90) - 0.5
    return StatForecast(np.minimum(p10, p50), p50, np.maximum(p90, p50), model, detail)


def clean_history(history: Sequence[float], as_of: date, events: Sequence[EventLike]) -> np.ndarray:
    """Divide out known historical context effects so the base model sees 'normal' demand."""
    h = np.asarray(history, dtype=float)
    origin = as_of - timedelta(days=len(h))
    m, _ = multipliers(origin, len(h), [e for e in events if e.source == "context"])
    return h / np.maximum(m, 1e-6)


def statistical_forecast(history: Sequence[float], as_of: date, horizon: int, events: Sequence[EventLike],
                         overrides: dict | None = None, extras: Sequence[EventLike] = ()):
    """Returns (p10, p50, p90, drivers, StatForecast). `overrides` maps context event id -> tuned multiplier."""
    base = statistical_base(clean_history(history, as_of, events), horizon)
    ov = overrides or {}
    applied = []
    for e in events:
        if e.end < as_of or e.start >= as_of + timedelta(days=horizon):
            continue
        mult = float(ov.get(_eid(e), e.multiplier)) if e.source == "context" else e.multiplier
        applied.append(EventLike(e.id, e.kind, e.start, e.end, mult, e.description, e.source))
    m, drivers = multipliers(as_of, horizon, list(applied) + list(extras))
    drivers = [base.detail + " on context-cleaned history"] + drivers
    r = lambda a: np.round(a * m, 2).tolist()  # noqa: E731
    return r(base.base_p10), r(base.base_p50), r(base.base_p90), drivers, base


def _eid(e: EventLike) -> str:
    return e.id if isinstance(e.id, str) else f"EV-{e.id}"


# ---------------------------------------------------------------- LLM adjustment layer

def adjustable_events(as_of: date, horizon: int, events: Sequence[EventLike]) -> list[EventLike]:
    end = as_of + timedelta(days=horizon)
    return [e for e in events if e.source == "context" and e.end >= as_of and e.start < end]


def validate_adjustments(obj: dict, adjustable: Sequence[EventLike], as_of: date, horizon: int) -> dict:
    priors = {_eid(e): e.multiplier for e in adjustable}
    em = obj.get("event_multipliers", {}) or {}
    if not isinstance(em, dict):
        raise ValueError("event_multipliers must be an object of event id -> multiplier")
    tuned = {}
    for k, v in em.items():
        if k not in priors:
            raise ValueError(f"unknown or non-adjustable event id {k!r} (confirmed signals are fixed)")
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(float(v)):
            raise ValueError(f"{k}: multiplier must be a number")
        v = float(v)
        lo = max(EVENT_MULT_RANGE[0], priors[k] * EVENT_PRIOR_RATIO[0])
        hi = min(EVENT_MULT_RANGE[1], priors[k] * EVENT_PRIOR_RATIO[1])
        if not lo - 1e-9 <= v <= hi + 1e-9:
            raise ValueError(f"{k}: multiplier {v:g} outside [{lo:.2f}, {hi:.2f}] (prior {priors[k]:g})")
        tuned[k] = v
    extras_raw = obj.get("extra_adjustments", []) or []
    if not isinstance(extras_raw, list) or len(extras_raw) > MAX_EXTRA:
        raise ValueError(f"extra_adjustments must be a list of at most {MAX_EXTRA}")
    last = as_of + timedelta(days=horizon - 1)
    extras = []
    for i, x in enumerate(extras_raw):
        if not isinstance(x, dict):
            raise ValueError("each extra adjustment must be an object")
        try:
            s, e = date.fromisoformat(str(x["start"])), date.fromisoformat(str(x["end"]))
            m = x["multiplier"]
        except (KeyError, ValueError) as exc:
            raise ValueError(f"extra adjustment {i}: needs start, end (YYYY-MM-DD) and multiplier") from exc
        if isinstance(m, bool) or not isinstance(m, (int, float)) or not EXTRA_MULT_RANGE[0] <= float(m) <= EXTRA_MULT_RANGE[1]:
            raise ValueError(f"extra adjustment {i}: multiplier must be within {EXTRA_MULT_RANGE}")
        if not (as_of <= s <= e <= last):
            raise ValueError(f"extra adjustment {i}: dates must lie within {as_of}..{last}")
        extras.append(EventLike(f"ADJ-{i + 1}", "adjustment", s, e, float(m),
                                f"Model adjustment: {str(x.get('reason', ''))[:120]}", "adjustment"))
    drivers = obj.get("drivers", []) or []
    if not isinstance(drivers, list):
        raise ValueError("drivers must be a list")
    return {"event_multipliers": tuned, "extras": extras, "drivers": [str(d)[:200] for d in drivers[:MAX_DRIVERS]]}


def build_summary(sku: dict, history: Sequence[float], as_of: date, horizon: int, events: Sequence[EventLike],
                  context_rows: Sequence[dict], baseline_p50: Sequence[float] | None = None) -> dict:
    h = np.asarray(history[-56:], dtype=float)
    start = as_of - timedelta(days=len(h))
    dow: dict[str, list[float]] = {}
    for i, v in enumerate(h):
        dow.setdefault(DOW[(start + timedelta(days=i)).weekday()], []).append(v)
    end = as_of + timedelta(days=horizon)
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
            "dow_mean": {k: round(float(np.mean(v)), 2) for k, v in dow.items()},
        },
        "statistical_baseline_p50": None if baseline_p50 is None else [round(float(x), 1) for x in baseline_p50],
        "adjustable_events": [
            {"id": _eid(e), "kind": e.kind, "start": e.start.isoformat(), "end": e.end.isoformat(),
             "prior_multiplier": round(e.multiplier, 3), "note": e.description}
            for e in adjustable_events(as_of, horizon, events)],
        "fixed_confirmed_events": [
            {"id": _eid(e), "type": e.kind, "start": e.start.isoformat(), "end": e.end.isoformat(),
             "multiplier": e.multiplier, "note": e.description}
            for e in events if e.source == "signal" and e.end >= as_of and e.start < end],
        "context": list(context_rows),
    }


# ---------------------------------------------------------------- orchestration

def forecast_one(llm: LLMClient | None, sku_id: str, summary: dict, history: Sequence[float], as_of: date,
                 horizon: int, events: Sequence[EventLike], h: str) -> ForecastOutput:
    adj = adjustable_events(as_of, horizon, events)
    res = run_json_task(
        llm, "forecast", prompts.FORECAST_SYSTEM, prompts.forecast_user(summary),
        lambda o: validate_adjustments(o, adj, as_of, horizon), hash_parts=(prompts.PROMPT_VERSION, STAT_VERSION),
        repair_attempts=1,
    )
    if res.ok and res.value:
        v = res.value
        p10, p50, p90, drivers, _ = statistical_forecast(history, as_of, horizon, events, v["event_multipliers"], v["extras"])
        tuned = [f"{k} x{m:.3g}" for k, m in v["event_multipliers"].items()]
        notes = v["drivers"] + ([f"Event multipliers tuned: {', '.join(tuned)}"] if tuned else [])
        return ForecastOutput(sku_id, p10, p50, p90, (notes + drivers)[:8], "hybrid", None, h, [res.record])
    p10, p50, p90, drivers, _ = statistical_forecast(history, as_of, horizon, events)
    return ForecastOutput(sku_id, p10, p50, p90, drivers, "statistical", res.error, h, [res.record])


def forecast_many(llm: LLMClient | None, jobs: list[dict], max_workers: int) -> list[ForecastOutput]:
    """jobs: dicts with keys sku_id, summary, history, as_of, horizon, events, hash."""
    def run(j):
        return forecast_one(llm, j["sku_id"], j["summary"], j["history"], j["as_of"], j["horizon"], j["events"], j["hash"])

    if llm is None or len(jobs) <= 1:
        return [run(j) for j in jobs]
    with ThreadPoolExecutor(max_workers=max(1, max_workers)) as ex:
        return list(ex.map(run, jobs))


def forecast_hash(summary: dict, model: str) -> str:
    return input_hash("forecast", prompts.PROMPT_VERSION, STAT_VERSION, model, summary)


def coverage(actual: Sequence[float], p10: Sequence[float], p90: Sequence[float]) -> float | None:
    a = np.asarray(actual, dtype=float)
    if not len(a):
        return None
    return float(np.mean((a >= np.asarray(p10) - 1e-9) & (a <= np.asarray(p90) + 1e-9)))

"""Value scorecard: replay plan and baselines in the same evaluation world(s); sensitivity; Monte Carlo; WAPE."""

from __future__ import annotations

from typing import Sequence

import numpy as np

from .world import Action, SkuContext, World, evaluate, sum_outcomes, transform_world


def evaluate_actions(ctxs: Sequence[SkuContext], actions: Sequence[Action], world: World) -> dict:
    by_sku = {a.sku_id: a for a in actions}
    outcomes = {}
    for ctx in ctxs:
        act = by_sku[ctx.sku_id]
        outcomes[ctx.sku_id] = evaluate(ctx, act, world.skus[ctx.sku_id])
    return {"totals": sum_outcomes(outcomes.values()), "per_sku": {k: v.to_dict() for k, v in outcomes.items()}}


def compare(ctxs: Sequence[SkuContext], plan: Sequence[Action], baseline: Sequence[Action], world: World,
            include_per_sku: bool = False) -> dict:
    """Both plans: same starting stock, budget and constraints, same demand path and lead times.
    Cash outlay is reported separately and is *not* part of total_loss."""
    p = evaluate_actions(ctxs, plan, world)
    b = evaluate_actions(ctxs, baseline, world)
    res = {
        "world": world.name,
        "plan": p["totals"],
        "baseline": b["totals"],
        "savings": round(b["totals"]["total_loss"] - p["totals"]["total_loss"], 2),
    }
    if include_per_sku:
        res["per_sku"] = {k: {"plan": p["per_sku"][k], "baseline": b["per_sku"][k]} for k in p["per_sku"]}
    return res


SENSITIVITY_CASES = (
    ("Demand −20%", 0.8, 0),
    ("Extra 5-day delay on all inbound", 1.0, 5),
)


def sensitivity(ctxs: Sequence[SkuContext], plan: Sequence[Action], baseline: Sequence[Action], world: World) -> list[dict]:
    """Decisions stay fixed (they were made today); only the world changes."""
    out = []
    for name, mult, delay in SENSITIVITY_CASES:
        w = transform_world(world, name, demand_mult=mult, extra_delay=delay)
        c = compare(ctxs, plan, baseline, w)
        out.append({"case": name, "plan_total_loss": c["plan"]["total_loss"],
                    "baseline_total_loss": c["baseline"]["total_loss"], "savings": c["savings"]})
    return out


def wape(actual: Sequence[float], forecast: Sequence[float]) -> float | None:
    a = np.asarray(actual, dtype=float)
    f = np.asarray(forecast, dtype=float)
    denom = float(np.abs(a).sum())
    if denom <= 0:
        return None
    return float(np.abs(a - f).sum() / denom)


# ---------------------------------------------------------------- service level

def hero_fill_rate(ctxs: Sequence[SkuContext], per_sku: dict) -> float | None:
    """Fill rate over priority (hero) SKUs only; per_sku: sku -> Outcome or Outcome.to_dict()."""
    dem = short = 0.0
    for c in ctxs:
        if c.priority != "hero" or c.sku_id not in per_sku:
            continue
        o = per_sku[c.sku_id]
        dem += o["demand_units"] if isinstance(o, dict) else o.demand_units
        short += o["units_short"] if isinstance(o, dict) else o.units_short
    return None if dem <= 0 else round(max(0.0, 1 - short / dem), 4)


# ---------------------------------------------------------------- Monte Carlo robustness

def _stats(x: Sequence[float]) -> dict:
    a = np.asarray(x, dtype=float)
    return {"mean": round(float(a.mean()), 2), "p5": round(float(np.percentile(a, 5)), 2),
            "p50": round(float(np.percentile(a, 50)), 2), "p95": round(float(np.percentile(a, 95)), 2)}


def monte_carlo(ctxs: Sequence[SkuContext], policies: dict[str, Sequence[Action]], worlds: Sequence[World],
                reference: str = "plan") -> dict:
    """Replay fixed decisions in many evaluation worlds. Reports loss / fill-rate distributions per policy and,
    for every other policy, the distribution of savings of `reference` against it and the win rate."""
    rows: dict[str, dict[str, list[float]]] = {k: {"loss": [], "fill": [], "hero_fill": [], "short": []}
                                               for k in policies}
    for w in worlds:
        for name, acts in policies.items():
            r = evaluate_actions(ctxs, acts, w)
            rows[name]["loss"].append(r["totals"]["total_loss"])
            rows[name]["fill"].append(r["totals"]["fill_rate"] or 0.0)
            rows[name]["hero_fill"].append(hero_fill_rate(ctxs, r["per_sku"]) or 0.0)
            rows[name]["short"].append(r["totals"]["units_short"])
    out = {"n_worlds": len(worlds), "policies": {}, "savings": {}}
    for name, r in rows.items():
        out["policies"][name] = {"total_loss": _stats(r["loss"]), "fill_rate": _stats(r["fill"]),
                                 "hero_fill_rate": _stats(r["hero_fill"]), "units_short": _stats(r["short"])}
    ref = np.asarray(rows[reference]["loss"])
    for name, r in rows.items():
        if name == reference:
            continue
        diff = np.asarray(r["loss"]) - ref
        out["savings"][f"{reference}_vs_{name}"] = {**_stats(diff), "win_rate": round(float(np.mean(diff > 0)), 4),
                                                    "loss_rate": round(float(np.mean(diff < 0)), 4)}
    return out

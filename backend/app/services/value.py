"""Value scorecard: replay plan and baseline in the same evaluation world; sensitivity; WAPE."""

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

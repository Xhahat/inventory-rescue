"""Extraction quality: run the real signal-extraction path (signals.extract, same prompt and validators) on a labelled
gold set of inbox messages and score it; then measure what the extracted signals are worth to the plan.

Matching is on event units (event_type, SKU): a gold or predicted event naming two SKUs is two units, so an extractor
that splits a multi-SKU event into one event per SKU is not penalised. Only validator-approved events count as
predictions (invalid ones would never reach the operator's confirm queue); "other" events are ignored.

Value of signals: for the gold messages behind evaluation-truth events (value_set), the plan is rebuilt with
(i) no signals, (ii) the LLM-extracted signals, (iii) the gold signals (perfect information), each treated as if the
operator confirmed it, and every plan is scored in the same evaluation world. Nothing is written to the signal tables:
the confirmed signals live only in an in-memory snapshot copy."""

from __future__ import annotations

import json
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import numpy as np
from sqlalchemy.orm import Session

from ..config import get_settings
from ..llm.cache import cached
from ..llm.client import LLMClient, get_llm
from ..models import AppState, utcnow
from . import forecasting as fc
from . import signals as sig
from . import state, value
from .planner import build_plan
from .world import ForecastBand, SkuContext

ACTIONABLE = ("supplier_delay", "demand_spike", "demand_drop", "expiry_risk")
PLANNING_TYPES = ("supplier_delay", "demand_spike", "demand_drop")
DATE_TOLERANCE_DAYS = 1
VALUE_MC_WORLDS = 40
SIGNAL_ID_BASE = 900_000


def gold_path() -> Path:
    return Path(get_settings().data_dir) / "eval" / "extraction_gold.jsonl"


def load_gold(path: Path | None = None) -> list[dict]:
    with open(path or gold_path(), encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


@dataclass
class Unit:
    event_type: str
    sku_id: str
    magnitude: float | None = None
    magnitude_range: list[float] | None = None
    start_date: str | None = None


def _units(events: list[dict]) -> list[Unit]:
    return [Unit(e["event_type"], s, e.get("magnitude"), e.get("magnitude_range"), e.get("start_date"))
            for e in events for s in e.get("sku_ids") or []]


def _pred_dict(ev: sig.ExtractedEvent) -> dict:
    return {"event_type": ev.event_type, "sku_ids": list(ev.sku_ids), "magnitude": ev.magnitude,
            "start_date": ev.start_date.isoformat() if ev.start_date else None,
            "end_date": ev.end_date.isoformat() if ev.end_date else None, "quoted_evidence": ev.quoted_evidence}


def _in_range(m: float | None, rng: list[float] | None) -> bool:
    return rng is None or (m is not None and rng[0] - 1e-9 <= float(m) <= rng[1] + 1e-9)


def _date_ok(pred: str | None, gold: str | None) -> bool:
    if gold is None:
        return True
    return pred is not None and abs((date.fromisoformat(pred) - date.fromisoformat(gold)).days) <= DATE_TOLERANCE_DAYS


def score_message(gold: dict, results: list[tuple], method: str) -> dict:
    """results: signals.extract output [(event | None, raw, rejection_reason)] for one message."""
    text = gold["text"]
    valid = [ev for ev, _, reason in results if reason is None and ev is not None and ev.event_type in ACTIONABLE]
    preds = _units([_pred_dict(e) for e in valid])
    optional = {(u.event_type, u.sku_id) for u in _units(gold.get("optional_events") or [])}
    golds = _units(gold.get("events") or [])
    used: set[int] = set()
    matched, misses, mag_checks, date_checks = [], [], [], []
    for g in golds:
        cands = [i for i, p in enumerate(preds) if i not in used and (p.event_type, p.sku_id) == (g.event_type, g.sku_id)]
        if not cands:
            misses.append({"event_type": g.event_type, "sku_id": g.sku_id, "expected_magnitude": g.magnitude})
            continue
        i = next((i for i in cands if _in_range(preds[i].magnitude, g.magnitude_range)), cands[0])
        used.add(i)
        p = preds[i]
        row = {"event_type": g.event_type, "sku_id": g.sku_id, "expected_magnitude": g.magnitude,
               "magnitude_range": g.magnitude_range, "predicted_magnitude": p.magnitude}
        if g.magnitude_range is not None:
            row["magnitude_ok"] = _in_range(p.magnitude, g.magnitude_range)
            mag_checks.append(row["magnitude_ok"])
        if g.start_date is not None and g.event_type in ("demand_spike", "demand_drop"):
            row["start_ok"] = _date_ok(p.start_date, g.start_date)
            row["expected_start"], row["predicted_start"] = g.start_date, p.start_date
            date_checks.append(row["start_ok"])
        matched.append(row)
    false_pos = [{"event_type": p.event_type, "sku_id": p.sku_id, "predicted_magnitude": p.magnitude}
                 for i, p in enumerate(preds) if i not in used and (p.event_type, p.sku_id) not in optional]
    evidence = [sig._norm_ws(str(raw.get("quoted_evidence") or "")) for _, raw, _ in results if isinstance(raw, dict)]
    evidence_ok = [len(q) >= 4 and q in sig._norm_ws(text) for q in evidence]
    invalid = [{"event_type": (ev.event_type if ev else str(raw.get("event_type", "?"))),
                "sku_ids": (ev.sku_ids if ev else raw.get("sku_ids")), "reason": reason}
               for ev, raw, reason in results if reason is not None]
    correct = not misses and not false_pos and all(mag_checks) and all(date_checks)
    return {"id": gold["id"], "tags": gold.get("tags", []), "expect_none": bool(gold.get("expect_none")),
            "method": method, "n_gold": len(golds), "n_pred": len(preds), "tp": len(matched),
            "fp": len(false_pos), "fn": len(misses), "matched": matched, "misses": misses,
            "false_positives": false_pos, "invalid": invalid, "magnitude_checks": mag_checks,
            "date_checks": date_checks, "evidence_ok": evidence_ok, "correct": correct,
            "predicted": [_pred_dict(e) for e in valid], "text": text}


def _ratio(a: float, b: float) -> float | None:
    return None if b == 0 else round(a / b, 4)


def aggregate(rows: list[dict]) -> dict:
    tp, fp, fn = (sum(r[k] for r in rows) for k in ("tp", "fp", "fn"))
    p, r = _ratio(tp, tp + fp), _ratio(tp, tp + fn)
    mags = [x for row in rows for x in row["magnitude_checks"]]
    dates = [x for row in rows for x in row["date_checks"]]
    evid = [x for row in rows for x in row["evidence_ok"]]
    none_rows = [row for row in rows if row["expect_none"]]
    n_raw = sum(len(row["evidence_ok"]) for row in rows)
    n_invalid = sum(len(row["invalid"]) for row in rows)
    by_tag: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        for t in row["tags"]:
            by_tag[t].append(row)
    return {
        "n_messages": len(rows), "n_gold_units": tp + fn, "n_predicted_units": tp + fp,
        "precision": p, "recall": r,
        "f1": None if not p or not r else round(2 * p * r / (p + r), 4),
        "magnitude_accuracy": _ratio(sum(mags), len(mags)), "n_magnitude_checks": len(mags),
        "start_date_accuracy": _ratio(sum(dates), len(dates)), "n_date_checks": len(dates),
        "evidence_valid_rate": _ratio(sum(evid), len(evid)), "n_raw_events": n_raw,
        "invalid_rate": _ratio(n_invalid, n_raw),
        "invalid_reasons": dict(Counter(i["reason"].split(":")[0] for row in rows for i in row["invalid"])),
        "false_positive_rate_no_event": _ratio(sum(1 for row in none_rows if row["fp"]), len(none_rows)),
        "n_no_event_messages": len(none_rows),
        "message_accuracy": _ratio(sum(row["correct"] for row in rows), len(rows)),
        "fallbacks": sum(1 for row in rows if row["method"] != "llm"),
        "by_tag": {t: {"n": len(v), "correct": sum(x["correct"] for x in v)} for t, v in sorted(by_tag.items())},
    }


# ---------------------------------------------------------------- extraction runs

def extraction_inputs(snap: state.Snapshot) -> tuple[list[dict], list[dict], list[dict], list[dict]]:
    """Same catalog / suppliers / open POs / lots the inbox ingest passes to the extractor."""
    catalog = [{"sku_id": p.sku_id, "name": p.name, "category": p.category, "supplier_id": p.supplier_id}
               for p in snap.products.values()]
    suppliers = [{"supplier_id": s.supplier_id, "name": s.name} for s in snap.suppliers.values()]
    open_pos = [{"po_id": o.po_id, "sku_id": o.sku_id, "supplier_id": o.supplier_id,
                 "expected_date": o.expected_date.isoformat()} for v in snap.open_pos.values() for o in v]
    lots = [{"lot_id": l.lot_id, "sku_id": l.sku_id, "expiry_date": l.expiry_date.isoformat()}
            for v in snap.lots.values() for l in v]
    return catalog, suppliers, open_pos, lots


def run_extractor(llm: LLMClient | None, gold: list[dict], snap: state.Snapshot, workers: int = 1) -> list[dict]:
    """Score every gold message with signals.extract (falls back to the heuristic extractor when llm is None or
    the model output is unusable, exactly like the inbox)."""
    catalog, suppliers, open_pos, lots = extraction_inputs(snap)

    def one(g: dict) -> dict:
        method, results, _ = sig.extract(llm, g["text"], catalog, suppliers, open_pos, lots, snap.as_of)
        return score_message(g, results, method)

    if workers <= 1:
        return [one(g) for g in gold]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(one, gold))


# ---------------------------------------------------------------- value of signals

def _signals(events: list[dict]) -> list[SimpleNamespace]:
    """Transient stand-ins for confirmed SignalEvent rows (int ids, as state.demand_events expects)."""
    out = []
    for e in events:
        if e["event_type"] not in PLANNING_TYPES or not e.get("sku_ids"):
            continue
        out.append(SimpleNamespace(
            id=SIGNAL_ID_BASE + len(out), event_type=e["event_type"], sku_ids=list(e["sku_ids"]),
            magnitude=float(e.get("magnitude") or 0.0),
            start_date=date.fromisoformat(e["start_date"]) if e.get("start_date") else None,
            end_date=date.fromisoformat(e["end_date"]) if e.get("end_date") else None))
    return out


def with_signals(snap: state.Snapshot, ctxs: list[SkuContext], signals: list) -> tuple[state.Snapshot,
                                                                                     list[SkuContext]]:
    """Inverse of state.without_signals: the signals become the confirmed set and their demand multipliers are
    applied to the (signal-free) forecast bands."""
    snap_s = replace(snap, confirmed=list(signals))
    out = []
    for ctx in ctxs:
        evs = [e for e in state.demand_events(snap_s, ctx.sku_id) if e.source == "signal"]
        if not evs:
            out.append(ctx)
            continue
        m, _ = fc.multipliers(snap.as_of, snap.horizon, evs)
        f = ctx.forecast
        band = ForecastBand(*(list(np.round(np.asarray(x) * m, 2)) for x in (f.p10, f.p50, f.p90)),
                            f.method, f.forecast_id, list(f.drivers))
        out.append(replace(ctx, forecast=band))
    return snap_s, out


def value_of_signals(snap: state.Snapshot, gold_rows: list[dict], scored: dict[str, dict],
                     budget: float | None = None) -> dict:
    s = get_settings()
    budget = float(s.default_budget_inr if budget is None else budget)
    snap0, ctxs0 = state.without_signals(snap, state.build_contexts(snap))
    sets = {"none": [],
            "llm": [e for g in gold_rows for e in scored[g["id"]]["predicted"]],
            "gold": [e for g in gold_rows for e in g["events"]]}
    ew = state.evaluation_world(snap0, ctxs0)
    worlds = state.monte_carlo_worlds(snap0, ctxs0, min(VALUE_MC_WORLDS, s.eval_worlds))
    policies, actual = {}, {}
    for name, events in sets.items():
        snap_s, ctxs_s = with_signals(snap0, ctxs0, _signals(events))
        acts = build_plan(ctxs_s, state.planning_world(snap_s, ctxs_s), budget, s.service_level_z).actions
        policies[name] = acts
        actual[name] = value.evaluate_actions(ctxs0, acts, ew)["totals"]["total_loss"]
    mc = value.monte_carlo(ctxs0, policies, worlds, reference="none")["policies"]
    mc_mean = {k: v["total_loss"]["mean"] for k, v in mc.items()}

    def captured(loss: dict) -> float | None:
        gain = loss["none"] - loss["gold"]
        return None if gain <= 0 else round((loss["none"] - loss["llm"]) / gain, 4)

    return {
        "messages": [g["id"] for g in gold_rows], "budget": budget,
        "n_signals": {k: len(_signals(v)) for k, v in sets.items()},
        "actual_world_loss": {k: round(v, 2) for k, v in actual.items()},
        "captured_actual": captured(actual),
        "mc_mean_loss": mc_mean, "captured_mc": captured(mc_mean), "n_mc_worlds": len(worlds),
        "note": ("Plan loss (lost margin + goodwill + waste + expedite premium; lower is better) in the seeded "
                 "evaluation world and the mean over Monte Carlo worlds, for plans built with no signals, with the "
                 "LLM-extracted signals and with the gold signals (perfect information) from the same messages. "
                 "captured = (loss_none - loss_llm) / (loss_none - loss_gold). Signals are applied in memory as if "
                 "confirmed; expiry_risk has no planning effect."),
    }


# ---------------------------------------------------------------- job entry point

def evaluate_extraction(db: Session, llm: LLMClient | None = None, include_value: bool = True) -> dict:
    """Full evaluation; the result is persisted in AppState 'extraction_eval'."""
    s = get_settings()
    gold = load_gold()
    snap = state.load_snapshot(db)
    client = cached(llm if llm is not None else get_llm(), "extraction")
    t0 = time.perf_counter()
    llm_rows = run_extractor(client, gold, snap, max(1, s.llm_max_concurrency)) if client is not None else []
    llm_secs = time.perf_counter() - t0
    heur_rows = run_extractor(None, gold, snap)
    result = {
        "gold_messages": len(gold), "as_of": snap.as_of.isoformat(),
        "model": getattr(client, "model", None),
        "llm": aggregate(llm_rows) if llm_rows else None,
        "heuristic": aggregate(heur_rows),
        "per_message": [{k: v for k, v in r.items() if k not in ("magnitude_checks", "date_checks", "evidence_ok")}
                        for r in llm_rows or heur_rows],
        "cache": {"hits": getattr(client, "hits", None), "misses": getattr(client, "misses", None)},
        "llm_seconds": round(llm_secs, 1),
        "computed_at": utcnow().isoformat(),
        "note": ("Event units = (event_type, SKU). Precision/recall/F1 over validator-approved events; magnitude "
                 "accuracy = matched units whose magnitude is inside the gold tolerance; start-date accuracy "
                 f"(+/-{DATE_TOLERANCE_DAYS} day) for demand events with a gold start; evidence-valid = raw model "
                 "events whose quote is verbatim in the message; false-positive rate = share of no-event messages "
                 "with at least one extracted event. 'heuristic' = the rule-based fallback extractor on the same "
                 "messages. The gold set was written by the developers and is small; treat it as a regression "
                 "check, not a benchmark."),
    }
    if include_value:
        value_rows = [g for g in gold if g.get("value_set")]
        scored = {r["id"]: r for r in (llm_rows or heur_rows)}
        result["value_of_signals"] = (value_of_signals(snap, value_rows, scored) if value_rows else None)
        if result["value_of_signals"] is not None:
            result["value_of_signals"]["extractor"] = "llm" if llm_rows else "heuristic"
    row = db.get(AppState, "extraction_eval")
    if row is None:
        db.add(AppState(key="extraction_eval", value=result))
    else:
        row.value = result
    db.commit()
    return result


def run_extraction_eval(db: Session) -> dict:
    """Background-job entry point: runs the evaluation and returns a compact summary for the job row."""
    r = evaluate_extraction(db)
    keys = ("precision", "recall", "f1", "magnitude_accuracy", "evidence_valid_rate", "false_positive_rate_no_event")
    vos = r.get("value_of_signals") or {}
    return {"gold_messages": r["gold_messages"], "computed_at": r["computed_at"],
            "llm": {k: r["llm"][k] for k in keys} if r["llm"] else None,
            "heuristic": {k: r["heuristic"][k] for k in keys},
            "captured_actual": vos.get("captured_actual"), "captured_mc": vos.get("captured_mc")}


def latest(db: Session) -> dict | None:
    row = db.get(AppState, "extraction_eval")
    return row.value if row is not None else None

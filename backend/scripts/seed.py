"""Deterministic seed data (INR) for the Inventory Rescue demo.

    python -m scripts.seed                # regenerate CSVs, reset DB, load, compute forecasts
    python -m scripts.seed --no-forecast  # skip forecasts (fallback is computed on the fly)

Story (as-of 2026-10-13, 30-day horizon, INR 50,000 budget):
  * QuickPack Organics (SUP-04) is unreliable; open POs PO-2041/PO-2042 will actually arrive 9 days late.
  * A fitness creator features Peanut Butter Crunchy 1kg (SKU-019): ~2.8x demand Oct 15-24.
  * Cold Brew Coffee lot LOT-013-A (420 units) expires Oct 25, far above expected sell-through.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.config import ROOT_DIR  # noqa: E402

SEED_DIR = ROOT_DIR / "data" / "seed"
LAST_SALE = date(2026, 10, 12)
AS_OF = LAST_SALE + timedelta(days=1)
HISTORY_DAYS = 120
HORIZON = 30
START = LAST_SALE - timedelta(days=HISTORY_DAYS - 1)
DOW = [0.92, 0.9, 0.95, 1.0, 1.05, 1.15, 1.1]
CHANNELS = [("D2C website", 0.35), ("Amazon", 0.30), ("Quick-commerce", 0.25), ("Retail", 0.10)]
SEGMENTS = [("new", 0.30), ("repeat", 0.55), ("subscription", 0.15)]

SUPPLIERS = [
    # id, name, stated LT, expedite LT, premium, terms, delay probs (on-time p, delay choices, weights), fill range
    ("SUP-01", "NutriSource Labs", 10, 4, 0.18, "Protein & supplements. Net 30. MOQ per SKU; air expedite +18%.",
     0.86, [1, 2], [0.7, 0.3], (0.97, 1.0)),
    ("SUP-02", "GreenField Foods", 8, 3, 0.20, "Spreads, muesli, granola. Net 21. Express truck +20%.",
     0.90, [1, 2, 3], [0.5, 0.3, 0.2], (0.97, 1.0)),
    ("SUP-03", "BrewWell Beverages", 6, 2, 0.15, "RTD beverages, tea. Net 15. Same-city express +15%.",
     0.92, [1], [1.0], (0.98, 1.0)),
    ("SUP-04", "QuickPack Organics", 7, 3, 0.25, "Snacks co-packer. Advance 50%. Express courier +25%.",
     0.55, [2, 3, 4, 5, 6, 7], [0.2, 0.2, 0.2, 0.2, 0.1, 0.1], (0.85, 1.0)),
]

# sku, name, category, supplier, cost, price, case_pack, moq, shelf_life, base daily demand, 120d trend multiplier
PRODUCTS = [
    ("SKU-001", "Whey Protein Chocolate 1kg", "Protein", "SUP-01", 1150, 1899, 4, 8, 365, 3.5, 1.05),
    ("SKU-002", "Whey Protein Vanilla 1kg", "Protein", "SUP-01", 1150, 1899, 4, 8, 365, 2.2, 1.0),
    ("SKU-003", "Plant Protein Unflavoured 500g", "Protein", "SUP-01", 640, 1099, 6, 12, 365, 1.8, 1.1),
    ("SKU-004", "Protein Bar Peanut Choco 6-pack", "Protein", "SUP-01", 210, 360, 12, 24, 180, 9.0, 1.1),
    ("SKU-005", "Protein Bar Almond Fudge 6-pack", "Protein", "SUP-01", 210, 360, 12, 24, 180, 6.0, 1.0),
    ("SKU-006", "Protein Cookies Oat 4-pack", "Protein", "SUP-01", 120, 220, 12, 24, 120, 7.0, 0.95),
    ("SKU-007", "Roasted Makhana Peri Peri 90g", "Snacks", "SUP-04", 55, 110, 24, 48, 120, 18.0, 1.15),
    ("SKU-008", "Roasted Makhana Himalayan Salt 90g", "Snacks", "SUP-04", 55, 110, 24, 48, 120, 14.0, 1.05),
    ("SKU-009", "Millet Chips Tomato 70g", "Snacks", "SUP-04", 35, 70, 30, 60, 90, 20.0, 1.1),
    ("SKU-010", "Ragi Crackers 150g", "Snacks", "SUP-04", 48, 95, 24, 48, 120, 10.0, 1.0),
    ("SKU-011", "Trail Mix Classic 200g", "Snacks", "SUP-04", 140, 260, 12, 24, 150, 8.0, 1.05),
    ("SKU-012", "Baked Chickpea Puffs 60g", "Snacks", "SUP-04", 30, 60, 30, 60, 90, 16.0, 0.85),
    ("SKU-013", "Cold Brew Coffee 250ml", "Beverages", "SUP-03", 60, 140, 24, 48, 60, 14.0, 1.0),
    ("SKU-014", "Kombucha Ginger Lemon 330ml", "Beverages", "SUP-03", 70, 150, 24, 48, 45, 9.0, 0.95),
    ("SKU-015", "Electrolyte Drink Orange 500ml", "Beverages", "SUP-03", 35, 80, 24, 48, 180, 15.0, 1.0),
    ("SKU-016", "Coconut Water 1L", "Beverages", "SUP-03", 65, 130, 12, 24, 90, 11.0, 1.0),
    ("SKU-017", "Green Tea Tulsi 25 bags", "Beverages", "SUP-03", 90, 185, 24, 24, 365, 6.0, 1.0),
    ("SKU-018", "Oat Milk Barista 1L", "Beverages", "SUP-03", 120, 230, 12, 24, 120, 8.0, 1.15),
    ("SKU-019", "Peanut Butter Crunchy 1kg", "Breakfast & Spreads", "SUP-02", 210, 499, 12, 12, 270, 8.0, 1.05),
    ("SKU-020", "Peanut Butter Creamy 1kg", "Breakfast & Spreads", "SUP-02", 260, 499, 12, 12, 270, 6.0, 1.0),
    ("SKU-021", "Almond Butter 500g", "Breakfast & Spreads", "SUP-02", 420, 799, 6, 12, 270, 3.0, 1.0),
    ("SKU-022", "Millet Muesli 400g", "Breakfast & Spreads", "SUP-02", 150, 299, 12, 24, 180, 9.0, 1.05),
    ("SKU-023", "Overnight Oats Berry 500g", "Breakfast & Spreads", "SUP-02", 140, 280, 12, 24, 180, 7.0, 1.0),
    ("SKU-024", "Honey Granola 450g", "Breakfast & Spreads", "SUP-02", 165, 320, 12, 24, 150, 6.0, 1.0),
    ("SKU-025", "Multivitamin Women 60 tabs", "Supplements", "SUP-01", 210, 499, 24, 24, 540, 4.0, 1.0),
    ("SKU-026", "Omega-3 Fish Oil 60 caps", "Supplements", "SUP-01", 260, 599, 24, 24, 540, 3.5, 1.05),
    ("SKU-027", "Vitamin D3 + K2 60 caps", "Supplements", "SUP-01", 160, 399, 24, 24, 540, 3.0, 1.0),
    ("SKU-028", "Ashwagandha 60 caps", "Supplements", "SUP-01", 140, 349, 24, 24, 540, 3.0, 0.9),
    ("SKU-029", "Apple Cider Vinegar Gummies 60", "Supplements", "SUP-01", 190, 449, 20, 20, 365, 5.0, 1.1),
    ("SKU-030", "Biotin Gummies 60", "Supplements", "SUP-01", 170, 399, 20, 20, 365, 3.5, 1.0),
]

# Starting position: days of cover on hand; optional open PO (days of demand, expected in N days, PO id).
COVER = {
    "SKU-001": 45, "SKU-002": 45, "SKU-003": 70, "SKU-004": 10, "SKU-005": 40, "SKU-006": 30, "SKU-007": 6,
    "SKU-008": 40, "SKU-009": 7, "SKU-010": 40, "SKU-011": 35, "SKU-012": 150, "SKU-013": None, "SKU-014": None,
    "SKU-015": 9, "SKU-016": 40, "SKU-017": 160, "SKU-018": 35, "SKU-019": 24, "SKU-020": 40, "SKU-021": 40,
    "SKU-022": 11, "SKU-023": 40, "SKU-024": 35, "SKU-025": 50, "SKU-026": 8, "SKU-027": 60, "SKU-028": 120,
    "SKU-029": 18, "SKU-030": 40,
}
OPEN_POS = {
    "SKU-007": (25, 3, "PO-2041"),
    "SKU-009": (20, 2, "PO-2042"),
    "SKU-004": (20, 4, "PO-2043"),
    "SKU-026": (30, 6, "PO-2044"),
}
# Explicit lots (qty, days to expiry) for the expiry stories.
SPECIAL_LOTS = {
    "SKU-013": [("LOT-013-A", 420, 12), ("LOT-013-B", 200, 50)],
    "SKU-014": [("LOT-014-A", 130, 8), ("LOT-014-B", 100, 38)],
    "SKU-006": [("LOT-006-A", 150, 15), ("LOT-006-B", 70, 100)],
}
# "Actual" outcome truth used only by the evaluation world (never shown to the planner).
TRUTH_PO_DELAYS = {"PO-2041": 9, "PO-2042": 9}
INFLUENCER = ("SKU-019", date(2026, 10, 15), date(2026, 10, 24), 2.8)


def context_events() -> list[dict]:
    d = date
    rows = [
        # history
        ("weather", d(2026, 6, 15), d(2026, 6, 30), None, "Beverages", 0.30, "Heatwave across north India; RTD beverage demand up"),
        ("weather", d(2026, 7, 1), d(2026, 9, 15), None, "Beverages", -0.10, "Monsoon season; cold beverages soften"),
        ("promotion", d(2026, 8, 13), d(2026, 8, 17), None, "Protein", 0.25, "Independence Day sale: 15% off protein"),
        ("holiday", d(2026, 8, 15), d(2026, 8, 15), None, None, 0.0, "Independence Day"),
        ("holiday", d(2026, 9, 12), d(2026, 9, 16), None, "Snacks", 0.25, "Ganesh Chaturthi festive snacking"),
        ("social", d(2026, 9, 1), d(2026, 9, 30), "SKU-007", None, 0.20, "#makhana trending on Instagram reels"),
        ("competitor", d(2026, 9, 20), d(2026, 10, 5), "SKU-001", None, -0.15, "Rival whey brand 20% off on Amazon"),
        ("competitor", d(2026, 9, 20), d(2026, 10, 5), "SKU-002", None, -0.15, "Rival whey brand 20% off on Amazon"),
        ("promotion", d(2026, 9, 25), d(2026, 9, 30), None, "Supplements", 0.20, "Wellness week bundle offer"),
        # horizon (known in advance)
        ("weather", d(2026, 10, 13), d(2026, 10, 31), None, "Beverages", 0.10, "Post-monsoon heat forecast (IMD)"),
        ("holiday", d(2026, 10, 18), d(2026, 10, 21), None, "Snacks", 0.20, "Dussehra / Navratri festive demand"),
        ("promotion", d(2026, 11, 1), d(2026, 11, 9), None, "Snacks", 0.35, "Diwali gift-box promotion: snacks 15% off"),
        ("holiday", d(2026, 11, 8), d(2026, 11, 8), None, None, 0.0, "Diwali"),
        ("competitor", d(2026, 10, 10), d(2026, 10, 25), "SKU-001", None, -0.10, "Rival whey festive discount 15%"),
    ]
    return [{"kind": k, "start_date": s.isoformat(), "end_date": e.isoformat(), "sku_id": sku or "",
             "category": cat or "", "value": v, "description": desc} for k, s, e, sku, cat, v, desc in rows]


def effect(ctx: list[dict], sku: str, cat: str, day: date) -> float:
    m = 1.0
    for c in ctx:
        if c["value"] == 0 or not (date.fromisoformat(c["start_date"]) <= day <= date.fromisoformat(c["end_date"])):
            continue
        if (c["sku_id"] and c["sku_id"] != sku) or (c["category"] and c["category"] != cat):
            continue
        if not c["sku_id"] and not c["category"]:
            continue
        m *= 1.0 + c["value"]
    return m


def lam(p: tuple, ctx: list[dict], day: date) -> float:
    sku, _, cat, _, _, _, _, _, _, base, trend = p
    t = (day - START).days
    level = base * trend ** ((t - HISTORY_DAYS) / HISTORY_DAYS)  # base = level at as-of
    return level * DOW[day.weekday()] * effect(ctx, sku, cat, day)


def generate(out: Path = SEED_DIR) -> None:
    rng = np.random.default_rng(20261013)
    out.mkdir(parents=True, exist_ok=True)
    ctx = context_events()

    def write(name: str, rows: list[dict]) -> None:
        with open(out / name, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)

    write("suppliers.csv", [{"supplier_id": s[0], "name": s[1], "stated_lead_time_days": s[2],
                             "expedite_lead_time_days": s[3], "expedite_premium_pct": s[4], "terms": s[5]}
                            for s in SUPPLIERS])
    write("products.csv", [{"sku_id": p[0], "name": p[1], "category": p[2], "supplier_id": p[3], "unit_cost": p[4],
                            "unit_price": p[5], "case_pack": p[6], "moq": p[7], "shelf_life_days": p[8],
                            "image_key": f"products/{p[0].lower()}.png"} for p in PRODUCTS])
    write("context_events.csv", ctx)

    sales = []
    for i in range(HISTORY_DAYS):
        day = START + timedelta(days=i)
        for p in PRODUCTS:
            units = int(rng.poisson(lam(p, ctx, day) * rng.lognormal(0, 0.12)))
            if units == 0:
                continue
            ch = rng.multinomial(units, [w for _, w in CHANNELS])
            for (cname, _), cu in zip(CHANNELS, ch):
                if cu == 0:
                    continue
                sg = rng.multinomial(cu, [w for _, w in SEGMENTS])
                for (sname, _), su in zip(SEGMENTS, sg):
                    if su:
                        sales.append({"sale_date": day.isoformat(), "sku_id": p[0], "channel": cname,
                                      "segment": sname, "units": int(su)})
    write("sales.csv", sales)

    # truth demand for the horizon (evaluation only)
    ev = []
    for i in range(HORIZON):
        day = AS_OF + timedelta(days=i)
        for p in PRODUCTS:
            x = lam(p, ctx, day)
            if p[0] == INFLUENCER[0] and INFLUENCER[1] <= day <= INFLUENCER[2]:
                x *= INFLUENCER[3]
            ev.append({"sku_id": p[0], "demand_date": day.isoformat(),
                       "units": int(rng.poisson(x * rng.lognormal(0, 0.12)))})
    write("evaluation_demand.csv", ev)

    lots, pos = [], []
    for p in PRODUCTS:
        sku, base, shelf = p[0], p[9], p[8]
        if sku in SPECIAL_LOTS:
            for lot_id, q, exp in SPECIAL_LOTS[sku]:
                lots.append({"lot_id": lot_id, "sku_id": sku, "qty": q,
                             "received_date": (AS_OF - timedelta(days=max(1, shelf - exp))).isoformat(),
                             "expiry_date": (AS_OF + timedelta(days=exp)).isoformat()})
        else:
            qty = int(round(COVER[sku] * base))
            older = int(round(qty * 0.4))
            for suffix, q, exp in (("A", older, max(45, int(shelf * 0.45))), ("B", qty - older, max(60, int(shelf * 0.85)))):
                if q > 0:
                    lots.append({"lot_id": f"LOT-{sku[-3:]}-{suffix}", "sku_id": sku, "qty": q,
                                 "received_date": (AS_OF - timedelta(days=max(1, shelf - exp))).isoformat(),
                                 "expiry_date": (AS_OF + timedelta(days=exp)).isoformat()})
        if sku in OPEN_POS:
            days, eta, po_id = OPEN_POS[sku]
            q = int(np.ceil(days * base / p[6]) * p[6])
            sup = next(s for s in SUPPLIERS if s[0] == p[3])
            pos.append({"po_id": po_id, "sku_id": sku, "supplier_id": p[3], "qty": q,
                        "order_date": (AS_OF + timedelta(days=eta - sup[2])).isoformat(),
                        "expected_date": (AS_OF + timedelta(days=eta)).isoformat(), "status": "open"})
    write("inventory_lots.csv", lots)
    write("purchase_orders.csv", pos)

    receipts = []
    rid = 0
    six_months_ago = AS_OF - timedelta(days=182)
    for p in PRODUCTS:
        sup = next(s for s in SUPPLIERS if s[0] == p[3])
        _, _, lt, _, _, _, p_on_time, choices, weights, fill = sup
        d = six_months_ago + timedelta(days=int(rng.integers(0, 10)))
        while True:
            expected = d + timedelta(days=lt)
            delay = 0 if rng.random() < p_on_time else int(rng.choice(choices, p=weights))
            received = expected + timedelta(days=delay)
            if received >= AS_OF:
                break
            ordered = int(max(p[7], np.ceil(p[9] * 18 / p[6]) * p[6]))
            rid += 1
            receipts.append({"receipt_id": f"RCV-{rid:05d}", "supplier_id": p[3], "sku_id": p[0],
                             "po_date": d.isoformat(), "expected_date": expected.isoformat(),
                             "received_date": received.isoformat(), "ordered_qty": ordered,
                             "received_qty": int(round(ordered * rng.uniform(*fill)))})
            d += timedelta(days=int(rng.integers(14, 22)))
    write("receipts.csv", receipts)
    (out / "evaluation_truth.json").write_text(json.dumps({"po_extra_delay_days": TRUTH_PO_DELAYS,
                                                           "as_of": AS_OF.isoformat()}, indent=2), encoding="utf-8")


def _read(name: str, d: Path) -> list[dict]:
    with open(d / name, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def load(src: Path = SEED_DIR) -> dict:
    from sqlalchemy import insert

    from app import db as dbm
    from app.models import (AppState, ContextEvent, EvaluationDemand, InventoryLot, Product, PurchaseOrder, Receipt,
                            Sale, Supplier)

    engine = dbm.get_engine()
    from app import models  # noqa: F401

    dbm.Base.metadata.drop_all(engine)
    dbm.init_db()
    D = date.fromisoformat
    counts = {}
    with engine.begin() as conn:
        def ins(model, rows):
            if rows:
                conn.execute(insert(model), rows)
            counts[model.__tablename__] = len(rows)

        ins(Supplier, [{**r, "stated_lead_time_days": int(r["stated_lead_time_days"]),
                        "expedite_lead_time_days": int(r["expedite_lead_time_days"]),
                        "expedite_premium_pct": float(r["expedite_premium_pct"])} for r in _read("suppliers.csv", src)])
        ins(Product, [{**r, "unit_cost": float(r["unit_cost"]), "unit_price": float(r["unit_price"]),
                       "case_pack": int(r["case_pack"]), "moq": int(r["moq"]),
                       "shelf_life_days": int(r["shelf_life_days"])} for r in _read("products.csv", src)])
        ins(Sale, [{"sale_date": D(r["sale_date"]), "sku_id": r["sku_id"], "channel": r["channel"],
                    "segment": r["segment"], "units": int(r["units"])} for r in _read("sales.csv", src)])
        ins(InventoryLot, [{**r, "qty": int(r["qty"]), "received_date": D(r["received_date"]),
                            "expiry_date": D(r["expiry_date"])} for r in _read("inventory_lots.csv", src)])
        ins(PurchaseOrder, [{**r, "qty": int(r["qty"]), "order_date": D(r["order_date"]),
                             "expected_date": D(r["expected_date"])} for r in _read("purchase_orders.csv", src)])
        ins(Receipt, [{**r, "po_date": D(r["po_date"]), "expected_date": D(r["expected_date"]),
                       "received_date": D(r["received_date"]), "ordered_qty": int(r["ordered_qty"]),
                       "received_qty": int(r["received_qty"])} for r in _read("receipts.csv", src)])
        ins(ContextEvent, [{"kind": r["kind"], "start_date": D(r["start_date"]), "end_date": D(r["end_date"]),
                            "sku_id": r["sku_id"] or None, "category": r["category"] or None,
                            "value": float(r["value"]), "description": r["description"]}
                           for r in _read("context_events.csv", src)])
        ins(EvaluationDemand, [{"sku_id": r["sku_id"], "demand_date": D(r["demand_date"]), "units": float(r["units"])}
                               for r in _read("evaluation_demand.csv", src)])
        truth = json.loads((src / "evaluation_truth.json").read_text(encoding="utf-8"))
        ins(AppState, [{"key": "evaluation_truth", "value": truth}])
    return counts


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-forecast", action="store_true", help="skip forecast computation")
    ap.add_argument("--no-generate", action="store_true", help="load existing CSVs without regenerating")
    ap.add_argument("--force-forecast", action="store_true", help="ignore the forecast cache")
    args = ap.parse_args(argv)
    if not args.no_generate:
        generate()
    counts = load()
    print("loaded:", json.dumps(counts))
    if not args.no_forecast:
        from app.db import session_factory
        from app.services import plans, state

        with session_factory()() as db:
            res = state.refresh_forecasts(db, force=args.force_forecast)
            methods = list(res["computed"].values())
            print(f"forecasts: computed={len(methods)} hybrid={methods.count('hybrid')} "
                  f"statistical={methods.count('statistical')} reused={len(res['reused'])}")
            print("alerts:", plans.recompute_alerts(db))


if __name__ == "__main__":
    main()

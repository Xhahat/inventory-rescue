"""Analytics adapter. Local: SQL aggregates + CSV export to var/exports. Later: BigQuery."""

from __future__ import annotations

import csv
import io
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Forecast, Plan, Product, Sale
from .storage import get_storage


def sales_by_channel_segment(db: Session, since: date) -> list[dict]:
    rows = db.execute(
        select(Product.category, Sale.channel, Sale.segment, func.sum(Sale.units),
               func.sum(Sale.units * Product.unit_price))
        .join(Product, Product.sku_id == Sale.sku_id)
        .where(Sale.sale_date >= since)
        .group_by(Product.category, Sale.channel, Sale.segment)
        .order_by(Product.category, Sale.channel, Sale.segment)
    ).all()
    return [{"category": c, "channel": ch, "segment": s, "units": int(u or 0), "revenue_inr": round(float(r or 0), 2)}
            for c, ch, s, u, r in rows]


def _csv(rows: list[dict]) -> bytes:
    if not rows:
        return b""
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue().encode("utf-8")


def export_tables(db: Session, as_of: date) -> list[str]:
    """Writes analytics-friendly CSVs (BigQuery load-ready)."""
    st = get_storage()
    keys = []
    keys.append(st.put(f"exports/analytics/sales_by_channel_segment_{as_of}.csv",
                       _csv(sales_by_channel_segment(db, as_of - timedelta(days=120)))))
    fc = [{"sku_id": f.sku_id, "as_of": f.as_of.isoformat(), "method": f.method, "day": i, "p10": f.p10[i],
           "p50": f.p50[i], "p90": f.p90[i]} for f in db.scalars(select(Forecast).where(Forecast.as_of == as_of))
          for i in range(len(f.p50))]
    keys.append(st.put(f"exports/analytics/forecasts_{as_of}.csv", _csv(fc)))
    plans = [{"plan_id": p.id, "as_of": p.as_of.isoformat(), "budget": p.budget, "status": p.status,
              "cash_used": p.summary.get("cash_used"), "expected_loss_plan": p.summary.get("expected_loss_plan"),
              "scorecard_savings": (p.summary.get("scorecard") or {}).get("savings")}
             for p in db.scalars(select(Plan).order_by(Plan.id))]
    keys.append(st.put(f"exports/analytics/plans_{as_of}.csv", _csv(plans)))
    return keys

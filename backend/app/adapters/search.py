"""Product/supplier discovery. Local: PostgreSQL trigram/full-text (SQLite: LIKE). Later: Vertex AI Search."""

from __future__ import annotations

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..db import is_postgres
from ..models import Product, Supplier


def search_products(db: Session, q: str, limit: int = 10) -> list[dict]:
    q = q.strip()[:100]
    if not q:
        return []
    if is_postgres():
        score = func.greatest(func.similarity(Product.name, q), func.similarity(Product.category, q),
                              func.similarity(Product.sku_id, q))
        stmt = (select(Product, score.label("score"))
                .where(or_(score > 0.15, Product.name.ilike(f"%{q}%"), Product.sku_id.ilike(f"%{q}%")))
                .order_by(score.desc()).limit(limit))
        rows = db.execute(stmt).all()
        return [{"sku_id": p.sku_id, "name": p.name, "category": p.category, "score": round(float(s), 3)} for p, s in rows]
    like = f"%{q}%"
    rows = db.scalars(select(Product).where(or_(Product.name.ilike(like), Product.sku_id.ilike(like),
                                                Product.category.ilike(like))).limit(limit))
    return [{"sku_id": p.sku_id, "name": p.name, "category": p.category, "score": 1.0} for p in rows]


def search_suppliers(db: Session, q: str, limit: int = 5) -> list[dict]:
    like = f"%{q.strip()[:100]}%"
    rows = db.scalars(select(Supplier).where(or_(Supplier.name.ilike(like), Supplier.terms.ilike(like),
                                                 Supplier.supplier_id.ilike(like))).limit(limit))
    return [{"supplier_id": s.supplier_id, "name": s.name, "terms": s.terms} for s in rows]

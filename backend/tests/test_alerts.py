from sqlalchemy import select

from app.models import Alert
from app.services import alerts, state


def test_alerts_cover_seed_story_and_dedupe(db):
    snap = state.load_snapshot(db)
    ctxs = state.build_contexts(snap)
    pw = state.planning_world(snap, ctxs)
    computed = alerts.compute_alerts(ctxs, pw, snap.as_of)
    types = {(a["entity_id"], a["alert_type"]) for a in computed}
    assert ("SKU-013", "expiry") in types
    assert any(t == "stockout_before_arrival" for _, t in types)
    assert ("SUP-04", "supplier_reliability") in types
    # one reliability alert per supplier even though several SKUs share it
    assert sum(1 for e, t in types if t == "supplier_reliability" and e == "SUP-04") == 1
    assert len(types) == len(computed)

    first = alerts.upsert_alerts(db, computed)
    second = alerts.upsert_alerts(db, computed)
    assert first["created"] == len(computed)
    assert second["created"] == 0 and second["updated"] == len(computed)
    rows = db.scalars(select(Alert)).all()
    assert len(rows) == len(computed)

    resolved = alerts.upsert_alerts(db, [a for a in computed if a["alert_type"] != "expiry"])
    assert resolved["resolved"] >= 1
    active = db.scalars(select(Alert).where(Alert.active.is_(True))).all()
    assert all(a.alert_type != "expiry" for a in active)

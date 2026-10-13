"""End-to-end API flow on a fresh seeded DB: message -> confirm -> job -> plan -> approve -> export, with a fake
LLM that returns valid extraction but malformed forecasts/explanations (so every fallback path is exercised)."""

import csv
import io
import json

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.config import ROOT_DIR


def _signal_handler(user: str) -> str:
    if "Peanut Butter Crunchy" in user:
        return json.dumps({"events": [
            {"event_type": "demand_spike", "sku_ids": ["SKU-019"], "magnitude": 3, "start_date": "2026-10-15",
             "end_date": "2026-10-24",
             "quoted_evidence": "Based on the last collab we expect roughly 3x normal demand for about 10 days."},
            {"event_type": "demand_spike", "sku_ids": ["SKU-999"], "magnitude": 2,
             "quoted_evidence": "we expect roughly 3x normal demand"},
            {"event_type": "demand_spike", "sku_ids": ["SKU-019"], "magnitude": 2,
             "quoted_evidence": "sales will double all quarter"},
        ]})
    return "not json at all"


@pytest.fixture()
def client(seeded_db):
    with TestClient(app) as c:
        yield c


def test_health_and_overview(client):
    assert client.get("/health").json()["status"] == "ok"
    ov = client.get("/api/overview").json()
    assert ov["as_of"] == "2026-10-13"
    skus = client.get("/api/skus").json()
    assert len(skus) == 30
    d = client.get("/api/skus/SKU-013").json()
    assert d["lots"] and d["forecast"]["method"] == "statistical" and d["reorder"]["reorder_point"] > 0
    assert client.get("/api/skus/NOPE").status_code == 404
    assert "SKU-019" in [p["sku_id"] for p in client.get("/api/search", params={"q": "peanut butter"}).json()["products"]]


def test_full_flow_with_fake_llm(client, fake_llm):
    fake = fake_llm(signal=_signal_handler)
    text = (ROOT_DIR / "data" / "messages" / "02_influencer_spike.txt").read_text(encoding="utf-8")
    res = client.post("/api/messages", json={"text": text, "source": "test"}).json()
    assert res["extraction_method"] == "llm"
    statuses = sorted((e["status"], e["rejection_reason"] or "") for e in res["events"])
    assert [s for s, _ in statuses] == ["invalid", "invalid", "pending"]
    assert any("unknown SKU" in r for _, r in statuses) and any("verbatim" in r for _, r in statuses)
    pending = next(e for e in res["events"] if e["status"] == "pending")
    invalid = next(e for e in res["events"] if e["status"] == "invalid")
    assert client.post(f"/api/signals/{invalid['id']}/confirm").status_code == 409

    plan_before = client.post("/api/plans", json={"budget": 50000}).json()

    conf = client.post(f"/api/signals/{pending['id']}/confirm").json()
    assert conf["signal"]["status"] == "confirmed" and conf["job"]
    job = client.get(f"/api/jobs/{conf['job']['id']}").json()
    assert job["status"] == "done", job
    assert job["result"]["computed"] == {"SKU-019": "statistical"}
    sku = client.get("/api/skus/SKU-019").json()
    assert sku["forecast"]["method"] == "statistical"
    assert sku["forecast"]["fallback_reason"].startswith("invalid output")
    assert any("EV-" in d for d in sku["forecast"]["drivers"])

    plan = client.post("/api/plans", json={"budget": 50000}).json()
    s = plan["summary"]
    assert s["cash_used"] <= 50000
    assert plan["explanation"]["method"] == "template" and plan["explanation"]["error"]
    assert s["scorecard"]["savings"] > 0
    assert s["scorecard"]["savings"] > plan_before["summary"]["scorecard"]["savings"]
    a19 = next(a for a in plan["actions"] if a["sku_id"] == "SKU-019")
    b19 = next((a for a in plan_before["actions"] if a["sku_id"] == "SKU-019"), {"order_qty": 0})
    assert a19["order_qty"] > b19["order_qty"]
    assert any(r.startswith("EV-") for r in a19["record_ids"])
    assert "forecast" in fake.calls and "explain" in fake.calls

    assert client.get(f"/api/plans/{plan['id']}/export/po").status_code == 404
    appr = client.post(f"/api/plans/{plan['id']}/approve").json()
    assert appr["status"] == "approved"
    po = list(csv.DictReader(io.StringIO(client.get(f"/api/plans/{plan['id']}/export/po").text)))
    md = list(csv.DictReader(io.StringIO(client.get(f"/api/plans/{plan['id']}/export/markdowns").text)))
    n_po = sum(1 for a in plan["actions"] if a["order_qty"])
    n_md = sum(1 for a in plan["actions"] if a["discount_pct"])
    assert len(po) == n_po and len(md) == n_md
    assert sum(float(r["line_total_inr"]) + float(r["expedite_premium_inr"]) for r in po) == pytest.approx(s["cash_used"], abs=1)
    assert client.get("/api/plans/latest").json()["id"] == plan["id"]

    calls = client.get("/api/llm/calls").json()
    assert {c["status"] for c in calls} >= {"ok", "invalid"}


def test_reject_signal_and_heuristic_path_without_llm(client):
    text = (ROOT_DIR / "data" / "messages" / "01_supplier_delay.txt").read_text(encoding="utf-8")
    res = client.post("/api/messages", json={"text": text, "source": "test"}).json()
    assert res["extraction_method"] == "heuristic" and res["llm_error"]
    ev = res["events"][0]
    assert ev["event_type"] == "supplier_delay" and ev["magnitude"] == 9
    r = client.post(f"/api/signals/{ev['id']}/reject", json={"reason": "already known"}).json()
    assert r["signal"]["status"] == "rejected" and r["job"] is None


def test_scenario_and_backtest(client):
    sc = client.post("/api/scenarios", json={"question": "What if demand rises 20% and QuickPack is delayed 7 days?",
                                             "budget": 50000}).json()
    assert sc["parse_method"] == "heuristic"
    assert sc["spec"]["demand_multiplier"] == pytest.approx(1.2)
    o = sc["outcomes_in_scenario"]
    assert o["adapted_plan"]["total_loss"] <= o["current_plan"]["total_loss"] + 1e-6
    assert sc["adapted_cash_used"] <= 50000

    cut = client.post("/api/scenarios", json={"question": "What if our budget is cut to 20000?", "budget": 50000}).json()
    assert cut["budget"] == 20000 and cut["current_budget"] == 50000
    assert cut["adapted_cash_used"] <= 20000 < cut["current_cash_used"]
    assert cut["changed_actions"]
    bad = client.post("/api/scenarios", json={"question": "What if demand for SKU-999 doubles?", "budget": 50000})
    assert bad.status_code == 400 and "SKU-999" in bad.json()["detail"]
    job = client.post("/api/backtest").json()
    assert client.get(f"/api/jobs/{job['id']}").json()["status"] == "done"
    bt = client.get("/api/backtest").json()
    assert bt["skus"] == 10 and bt["hybrid_valid_forecasts"] == 0 and len(bt["per_origin"]) == 3
    assert bt["pooled"]["statistical"]["wape"] > 0 and 0 < bt["pooled"]["statistical"]["coverage_p10_p90"] <= 1
    assert "hybrid" not in bt["pooled"]


def test_plan_robustness_frontier_overrides_and_diff(client):
    plan = client.post("/api/plans", json={"budget": 50000}).json()
    s = plan["summary"]
    assert s["diff"] is None and s["robustness"]["status"] == "queued"
    fr = s["frontier"]
    assert [r["budget"] for r in fr] == sorted(r["budget"] for r in fr) and any(r["is_current"] for r in fr)
    assert all(a["planning_objective"] >= b["planning_objective"] - 1e-6 for a, b in zip(fr, fr[1:]))
    assert fr[0]["cash_used"] == 0 and all(r["cash_used"] <= r["budget"] + 1e-6 for r in fr)
    assert s["service"]["hero_skus"] and "plan_hero_fill_rate" in s["service"]
    assert s["scorecard"]["savings_vs_tuned"] == pytest.approx(
        s["scorecard"]["tuned_baseline"]["total_loss"] - s["scorecard"]["plan"]["total_loss"], abs=0.02)

    full = client.get(f"/api/plans/{plan['id']}").json()["summary"]["robustness"]
    assert full["status"] == "done", full
    mc = full["monte_carlo"]
    assert mc["n_worlds"] == 8 and {"plan", "static_baseline", "tuned_baseline"} <= set(mc["policies"])
    assert 0 <= mc["savings"]["plan_vs_static_baseline"]["win_rate"] <= 1
    roll = full["rolling"]
    assert set(roll["actual_world"]) == {"plan_once", "replan_weekly", "baseline_weekly"}
    assert roll["actual_world"]["plan_once"]["total_loss"] == pytest.approx(s["scorecard"]["plan"]["total_loss"], abs=1)
    assert full["signal_ablation"]["status"] == "no confirmed signals"

    target = next(a for a in plan["actions"] if a["order_qty"])
    other = next(a for a in plan["actions"] if a["sku_id"] != target["sku_id"])
    too_much = client.post(f"/api/plans/{plan['id']}/overrides",
                           json={"items": [{"sku_id": target["sku_id"], "order_qty": 1_000_000}]})
    assert too_much.status_code == 400 and "budget" in too_much.json()["detail"]
    bad = client.post(f"/api/plans/{plan['id']}/overrides", json={"items": [{"sku_id": "SKU-001", "discount_pct": 0.8}]})
    assert bad.status_code == 400
    edited = client.post(f"/api/plans/{plan['id']}/overrides",
                         json={"items": [{"sku_id": target["sku_id"], "order_qty": 1}, {"sku_id": other["sku_id"],
                                                                                         "skip": True}]}).json()
    row = next(a for a in edited["actions"] if a["sku_id"] == target["sku_id"])
    assert row["overridden"] and row["order_qty"] >= 1 and row["order_qty"] % 1 == 0 and row["original"]
    assert other["sku_id"] not in {a["sku_id"] for a in edited["actions"]}
    assert other["sku_id"] in {x["sku_id"] for x in edited["summary"]["overridden_removed"]}
    assert edited["summary"]["revision"] == 1 and edited["summary"]["overrides"]
    assert edited["summary"]["cash_used"] <= 50000

    nxt = client.post("/api/plans", json={"budget": 30000}).json()
    d = nxt["summary"]["diff"]
    assert d["previous_plan_id"] == plan["id"] and d["previous_budget"] == 50000
    assert d["n_added"] + d["n_removed"] + d["n_changed"] == len(d["changes"])
    client.post(f"/api/plans/{plan['id']}/approve")
    assert client.post(f"/api/plans/{plan['id']}/overrides",
                       json={"items": [{"sku_id": "SKU-001", "skip": True}]}).status_code == 409


def test_sku_priority_override_changes_goodwill(client):
    before = client.get("/api/skus/SKU-030").json()["priority"]
    assert before["priority"] in ("hero", "standard") and not before["overridden"]
    r = client.put("/api/skus/SKU-030/priority", json={"priority": "hero"}).json()
    assert r["priority"] == "hero" and r["overridden"]
    after = client.get("/api/skus/SKU-030").json()["priority"]
    assert after["goodwill_per_unit"] >= before["goodwill_per_unit"]
    assert next(s for s in client.get("/api/skus").json() if s["sku_id"] == "SKU-030")["priority"] == "hero"
    assert client.put("/api/skus/SKU-030/priority", json={"priority": "auto"}).json()["overridden"] is False
    assert client.put("/api/skus/SKU-030/priority", json={"priority": "vip"}).status_code == 422
    assert client.put("/api/skus/NOPE/priority", json={"priority": "hero"}).status_code == 404


def test_csv_import_is_all_or_nothing(client):
    bad = "sale_date,sku_id,channel,segment,units\n2026-10-13,SKU-001,online,retail,5\n2026-10-13,SKU-999,online,retail,5\n"
    r = client.post("/api/import/sales", files={"file": ("s.csv", bad, "text/csv")})
    assert r.status_code == 400
    assert client.get("/api/overview").json()["as_of"] == "2026-10-13"
    good = "sale_date,sku_id,channel,segment,units\n2026-10-13,SKU-001,online,retail,5\n"
    r = client.post("/api/import/sales", files={"file": ("s.csv", good, "text/csv")})
    assert r.status_code == 200 and r.json()["imported"] == 1
    assert client.get("/api/overview").json()["as_of"] == "2026-10-14"
    assert client.post("/api/import/sales", files={"file": ("s.csv", "a,b\n1,2\n", "text/csv")}).status_code == 400


def test_message_validation(client):
    assert client.post("/api/messages", json={"text": "", "source": "x"}).status_code == 422
    assert client.post("/api/messages", json={"text": "x" * 9000, "source": "x"}).status_code == 422

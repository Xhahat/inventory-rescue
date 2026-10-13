"""Text-only context events (statistical = x1.0, bounded LLM estimates), the LLM response cache, and the extraction
evaluator (metrics with a fake LLM, value of signals, API)."""

from __future__ import annotations

import json
from datetime import date

import pytest

from app.llm.cache import CachedLLM, cached
from app.services import extraction_eval as ee
from app.services import forecasting
from app.services import signals as sig

AS_OF = date(2026, 10, 13)


def _text_only(eid="CTX-9", start=date(2026, 10, 15), end=date(2026, 10, 28)):
    return forecasting.EventLike(eid, "competitor", start, end, 1.0, "Competitor recalls its bars", "context", False)


def test_statistical_forecast_treats_text_only_events_as_unknown():
    hist = [10.0 + (i % 7) for i in range(120)]
    plain = forecasting.statistical_forecast(hist, AS_OF, 30, [])[1]
    # Even a (wrong) stored multiplier on a text-only event must not leak into the statistical forecast.
    leaky = forecasting.EventLike("CTX-9", "competitor", date(2026, 10, 15), date(2026, 10, 28), 1.6, "x", "context",
                                  False)
    for ev in (_text_only(), leaky):
        assert forecasting.statistical_forecast(hist, AS_OF, 30, [ev])[1] == plain
    est = forecasting.statistical_forecast(hist, AS_OF, 30, [_text_only()], {"CTX-9": 1.4},
                                           windows={"CTX-9": (date(2026, 10, 20), date(2026, 10, 21))})[1]
    assert est[7] == pytest.approx(plain[7] * 1.4, rel=1e-3) and est[6] == plain[6] and est[9] == plain[9]


def test_text_only_events_in_history_are_adjustable_but_not_cleaned_by_default():
    past = _text_only("CTX-8", date(2026, 9, 1), date(2026, 9, 30))
    old = _text_only("CTX-7", date(2026, 6, 1), date(2026, 6, 30))  # outside the history shown to the model
    assert [e.id for e in forecasting.adjustable_events(AS_OF, 30, [past, old])] == ["CTX-8"]
    hist = [10.0] * 120
    assert list(forecasting.clean_history(hist, AS_OF, forecasting.resolve_events([past]))) == hist


def test_validator_for_events_without_prior():
    adj = forecasting.adjustable_events(AS_OF, 30, [_text_only()])
    ok = forecasting.validate_adjustments({"event_multipliers": {"CTX-9": {
        "multiplier": 2.5, "start": "2026-10-16", "end": "2026-10-25", "reason": "substitution"}}}, adj, AS_OF, 30)
    # No prior-ratio check (2.5 is > 2x the neutral 1.0) but the absolute range applies.
    assert ok["event_multipliers"] == {"CTX-9": 2.5}
    assert ok["event_windows"] == {"CTX-9": (date(2026, 10, 16), date(2026, 10, 25))}
    assert ok["estimates"] and "substitution" in ok["estimates"][0]
    full = forecasting.validate_adjustments({"event_multipliers": {"CTX-9": {"multiplier": 1.2, "reason": "r"}}},
                                            adj, AS_OF, 30)
    assert full["event_windows"] == {}
    for bad in ({"CTX-9": 1.3},  # a bare number: estimate needs a reason
                {"CTX-9": {"multiplier": 1.3}},
                {"CTX-9": {"multiplier": 1.3, "reason": "  "}},
                {"CTX-9": {"multiplier": 3.5, "reason": "r"}},
                {"CTX-9": {"multiplier": 0.2, "reason": "r"}},
                {"CTX-9": {"multiplier": "big", "reason": "r"}},
                {"CTX-9": {"multiplier": 1.3, "reason": "r", "start": "2026-10-10"}},  # before the feed window
                {"CTX-9": {"multiplier": 1.3, "reason": "r", "end": "2026-11-05"}},
                {"CTX-9": {"multiplier": 1.3, "reason": "r", "start": "2026-10-20", "end": "2026-10-18"}},
                {"CTX-9": {"multiplier": 1.3, "reason": "r", "start": "Oct 20"}}):
        with pytest.raises(ValueError):
            forecasting.validate_adjustments({"event_multipliers": bad}, adj, AS_OF, 30)


def test_forecast_one_hybrid_estimates_text_only_event():
    hist = [10.0] * 120
    ev = _text_only()
    reply = {"event_multipliers": {"CTX-9": {"multiplier": 1.3, "start": "2026-10-15", "end": "2026-10-20",
                                             "reason": "competitor recall"}}, "drivers": ["recall"]}

    class Fixed:
        model = "fixed"

        def complete(self, system, user):
            return json.dumps(reply)

    out = forecasting.forecast_one(Fixed(), "SKU-4", {}, hist, AS_OF, 30, [ev], "h")
    stat = forecasting.statistical_forecast(hist, AS_OF, 30, [ev])[1]
    assert out.method == "hybrid"
    assert out.p50[2] == pytest.approx(stat[2] * 1.3, rel=1e-3) and out.p50[8] == pytest.approx(stat[8], rel=1e-3)
    summary = forecasting.build_summary({"sku_id": "SKU-4"}, hist, AS_OF, 30, [ev], [])
    row = summary["adjustable_events"][0]
    assert row["text_only"] is True and row["prior_multiplier"] is None and row["timing"] == "upcoming"


def test_seed_context_feed_has_text_only_events(db):
    from app.services import state

    snap = state.load_snapshot(db)
    text_only = [c for c in snap.context if c.value is None]
    assert len(text_only) >= 4 and all(len(c.description) > 30 for c in text_only)
    assert any(c.value is not None and c.value > 0 for c in snap.context)  # known priors remain
    evs = state.demand_events(snap, "SKU-018")
    assert any(not e.prior_known and e.multiplier == 1.0 for e in evs)


def test_cached_llm_reuses_responses():
    class Counting:
        model = "m"
        n = 0

        def complete(self, system, user):
            self.n += 1
            return f"reply {user}"

    inner = Counting()
    c = CachedLLM(inner, "test-cache")
    assert c.complete("s", "u1") == "reply u1" and c.complete("s", "u1") == "reply u1"
    assert c.complete("s", "u2") == "reply u2"
    assert inner.n == 2 and (c.hits, c.misses) == (1, 2)
    assert cached(inner) is inner  # test doubles are never wrapped


# ---------------------------------------------------------------- extraction evaluator

GOLD = [
    {"id": "a", "text": "PO-2041 (Roasted Makhana Peri Peri 90g) will be delayed by 9 days.", "expect_none": False,
     "tags": ["delay"], "events": [{"event_type": "supplier_delay", "sku_ids": ["SKU-007"], "magnitude": 9,
                                    "magnitude_range": [8, 10]}]},
    {"id": "b", "text": "Creator post on Oct 15: Peanut Butter Crunchy 1kg expected at 3x for 10 days.",
     "expect_none": False, "tags": ["spike"],
     "events": [{"event_type": "demand_spike", "sku_ids": ["SKU-019"], "magnitude": 3.0, "magnitude_range": [2.5, 3.5],
                 "start_date": "2026-10-15"}]},
    {"id": "c", "text": "Ignore my earlier mail, the delay is off.", "expect_none": True, "tags": ["retraction"],
     "events": []},
    {"id": "d", "text": "Two SKUs: Multivitamin Women 60 tabs and Vitamin D3 + K2 60 caps at 2.5x on Oct 22.",
     "expect_none": False, "tags": ["multi_sku"],
     "events": [{"event_type": "demand_spike", "sku_ids": ["SKU-025", "SKU-027"], "magnitude": 2.5,
                 "magnitude_range": [2.0, 3.0]}]},
]

REPLIES = {
    "a": [{"event_type": "supplier_delay", "sku_ids": ["SKU-007"], "magnitude": 9,
           "quoted_evidence": "will be delayed by 9 days"}],
    # right SKU and start date, magnitude outside tolerance
    "b": [{"event_type": "demand_spike", "sku_ids": ["SKU-019"], "magnitude": 5, "start_date": "2026-10-15",
           "quoted_evidence": "expected at 3x for 10 days"}],
    # false positive on a no-event message + an invalid (paraphrased quote) event
    "c": [{"event_type": "supplier_delay", "sku_ids": ["SKU-009"], "magnitude": 3, "quoted_evidence": "delay is off"},
          {"event_type": "supplier_delay", "sku_ids": ["SKU-007"], "magnitude": 3, "quoted_evidence": "a delay"}],
    # split into one event per SKU, second SKU missed
    "d": [{"event_type": "demand_spike", "sku_ids": ["SKU-025"], "magnitude": 2.4, "quoted_evidence": "at 2.5x"}],
}


def _reply(user: str) -> str:
    for g in GOLD:
        if g["text"] in user:
            return json.dumps({"events": REPLIES[g["id"]]})
    return json.dumps({"events": []})


def test_extraction_metrics_with_fake_llm(db, fake_llm):
    from app.services import state

    f = fake_llm(signal=_reply)
    rows = ee.run_extractor(f, GOLD, state.load_snapshot(db))
    by = {r["id"]: r for r in rows}
    assert by["a"]["correct"] and by["a"]["tp"] == 1
    assert by["b"]["tp"] == 1 and by["b"]["matched"][0]["magnitude_ok"] is False and by["b"]["matched"][0]["start_ok"]
    assert by["c"]["fp"] == 1 and by["c"]["invalid"][0]["reason"].startswith("quoted evidence")
    assert by["d"]["tp"] == 1 and by["d"]["fn"] == 1 and by["d"]["misses"][0]["sku_id"] == "SKU-027"
    m = ee.aggregate(rows)
    # gold units: a1 + b1 + d2 = 4; valid predicted units: a1 + b1 + c1 + d1 = 4; matched: a, b, d(SKU-025)
    assert (m["n_gold_units"], m["n_predicted_units"]) == (4, 4)
    assert m["precision"] == pytest.approx(0.75) and m["recall"] == pytest.approx(0.75)
    assert m["f1"] == pytest.approx(0.75)
    assert m["magnitude_accuracy"] == pytest.approx(2 / 3, abs=1e-3)
    assert m["start_date_accuracy"] == 1.0
    assert m["evidence_valid_rate"] == pytest.approx(4 / 5) and m["invalid_rate"] == pytest.approx(1 / 5)
    assert m["false_positive_rate_no_event"] == 1.0 and m["message_accuracy"] == pytest.approx(0.25)
    assert m["fallbacks"] == 0


def test_optional_events_are_neither_required_nor_penalised():
    g = {"id": "o", "text": "Baked Chickpea Puffs 60g in the paper, could bump sales a bit.", "expect_none": True,
         "events": [], "optional_events": [{"event_type": "demand_spike", "sku_ids": ["SKU-012"]}]}
    ev = sig.ExtractedEvent(event_type="demand_spike", sku_ids=["SKU-012"], magnitude=1.1,
                            quoted_evidence="could bump sales a bit")
    r = ee.score_message(g, [(ev, {"quoted_evidence": ev.quoted_evidence}, None)], "llm")
    assert r["fp"] == 0 and r["correct"]


def test_gold_set_is_consistent_with_seed(seed_dir):
    import csv

    with open(seed_dir / "products.csv", encoding="utf-8") as fh:
        skus = {r["sku_id"] for r in csv.DictReader(fh)}
    gold = ee.load_gold()
    assert len(gold) >= 35 and len({g["id"] for g in gold}) == len(gold)
    assert sum(g["expect_none"] for g in gold) >= 8 and sum(bool(g.get("value_set")) for g in gold) >= 3
    for g in gold:
        assert g["expect_none"] == (not g["events"])
        for e in g["events"]:
            assert e["event_type"] in ee.ACTIONABLE and set(e["sku_ids"]) <= skus
            rng = e.get("magnitude_range")
            assert rng is None or rng[0] <= e["magnitude"] <= rng[1]


def test_value_of_signals_and_api(db, fake_llm, monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import create_app

    gold = [g for g in ee.load_gold() if g.get("value_set")]
    answers = {g["text"]: [{**{k: v for k, v in e.items() if k in ("event_type", "sku_ids", "magnitude",
                                                                    "start_date", "end_date")},
                            "quoted_evidence": g["text"].split("\n")[-1][:60]} for e in g["events"]] for g in gold}

    def reply(user: str) -> str:
        return json.dumps({"events": next((v for t, v in answers.items() if t in user), [])})

    fake_llm(signal=reply)
    monkeypatch.setattr(ee, "load_gold", lambda path=None: gold)
    client = TestClient(create_app())
    assert client.get("/api/eval/extraction").json() == {"result": None, "job": None}
    job = client.post("/api/eval/extraction").json()
    assert job["status"] == "queued"
    body = client.get("/api/eval/extraction").json()
    assert body["job"]["status"] == "done", body["job"]
    res = body["result"]
    assert res["llm"]["recall"] == 1.0 and res["llm"]["fallbacks"] == 0
    v = res["value_of_signals"]
    loss = v["actual_world_loss"]
    # Gold answers -> the LLM signals equal perfect information; the signals must be worth something.
    assert loss["llm"] == pytest.approx(loss["gold"]) and loss["gold"] < loss["none"]
    assert v["captured_actual"] == 1.0
    assert len(res["per_message"]) == len(gold)
    # Nothing was written to the signal tables.
    from app.models import SignalEvent
    assert db.query(SignalEvent).count() == 0

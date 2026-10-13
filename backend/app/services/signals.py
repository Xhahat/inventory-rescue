"""Signal extraction: message -> validated events awaiting operator confirmation."""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Literal

from pydantic import BaseModel, Field, ValidationError, field_validator

from ..llm import prompts
from ..llm.client import LLMClient, run_json_task

EventType = Literal["supplier_delay", "demand_spike", "demand_drop", "expiry_risk", "other"]


class ExtractedEvent(BaseModel):
    event_type: EventType
    sku_ids: list[str] = Field(default_factory=list)
    magnitude: float = 0.0
    start_date: date | None = None
    end_date: date | None = None
    quoted_evidence: str = ""

    @field_validator("sku_ids", mode="before")
    @classmethod
    def _norm_ids(cls, v):
        if v is None:
            return []
        if isinstance(v, str):
            v = [v]
        return [str(x).strip().upper() for x in v]


class Extraction(BaseModel):
    events: list[dict] = Field(default_factory=list)


def _norm_ws(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def check_event(raw: dict, source_text: str, known_skus: set[str]) -> tuple[ExtractedEvent | None, dict, str | None]:
    """Returns (event, raw_clean, reason). reason is None when the event is valid."""
    try:
        ev = ExtractedEvent.model_validate(raw)
    except ValidationError as exc:
        return None, raw, f"schema: {exc.errors()[0].get('msg', 'invalid')}"
    if not ev.sku_ids:
        return ev, raw, "no SKU identified"
    unknown = [s for s in ev.sku_ids if s not in known_skus]
    if unknown:
        return ev, raw, f"unknown SKU(s): {', '.join(unknown)}"
    q = _norm_ws(ev.quoted_evidence)
    if len(q) < 4 or q not in _norm_ws(source_text):
        return ev, raw, "quoted evidence is not a verbatim substring of the message"
    if ev.start_date and ev.end_date and ev.end_date < ev.start_date:
        return ev, raw, "end_date before start_date"
    if ev.event_type == "supplier_delay" and not (0 < ev.magnitude <= 90):
        return ev, raw, "supplier delay must be 1..90 days"
    if ev.event_type == "demand_spike" and not (1 < ev.magnitude <= 10):
        return ev, raw, "demand spike multiplier must be in (1, 10]"
    if ev.event_type == "demand_drop" and not (0 < ev.magnitude < 1):
        return ev, raw, "demand drop multiplier must be in (0, 1)"
    if ev.magnitude < 0:
        return ev, raw, "negative magnitude"
    return ev, raw, None


def validate_extraction(obj: dict, source_text: str, known_skus: set[str]) -> list[tuple[ExtractedEvent | None, dict, str | None]]:
    data = Extraction.model_validate(obj)
    return [check_event(e if isinstance(e, dict) else {}, source_text, known_skus) for e in data.events[:20]]


# ---------------------------------------------------------------- heuristic fallback (no LLM)

_SENT = re.compile(r"(?:[^.!?\n]|[.!?](?=[^\s]))+[.!?]?")
_GENERIC = {"g", "ml", "kg", "pack", "the", "and", "of", "with", "x"}
_MONTHS = {m: i + 1 for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov",
                                           "dec"])}
_MONTH_RE = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"


def _tokens(s: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9]+", s.lower()) if t not in _GENERIC and not re.fullmatch(r"\d+(g|ml|kg)?", t)]


def _match_skus(sentence: str, catalog: list[dict], open_pos: list[dict], lots: list[dict]) -> list[str]:
    found: list[str] = []
    up = sentence.upper()
    for m in re.findall(r"SKU-\d{3}", up):
        found.append(m)
    for po in open_pos:
        if po["po_id"].upper() in up:
            found.append(po["sku_id"])
    for lot in lots:
        if lot["lot_id"].upper() in up:
            found.append(lot["sku_id"])
    stoks = set(_tokens(sentence))
    for p in catalog:
        nt = _tokens(p["name"])
        if nt and sum(t in stoks for t in nt) / len(nt) >= 0.75:
            found.append(p["sku_id"])
    return list(dict.fromkeys(found))


def _month_dates(sentence: str, as_of: date) -> list[date]:
    out = []
    pat = rf"\b{_MONTH_RE}\s+(\d{{1,2}})\b|\b(\d{{1,2}})(?:st|nd|rd|th)?\s+{_MONTH_RE}"
    for m in re.finditer(pat, sentence, re.I):
        mon = _MONTHS[(m.group(1) or m.group(4)).lower()[:3]]
        day = int(m.group(2) or m.group(3))
        year = as_of.year
        try:
            d = date(year, mon, day)
            if d < as_of - timedelta(days=180):
                d = date(year + 1, mon, day)
        except ValueError:
            continue
        out.append(d)
    return out


def _dates(sentence: str, as_of: date, carry_start: date | None = None) -> tuple[date | None, date | None]:
    found = [date.fromisoformat(x) for x in re.findall(r"\b(20\d\d-\d\d-\d\d)\b", sentence)]
    found += _month_dates(sentence, as_of)
    if len(found) >= 2:
        return found[0], found[1]
    start = found[0] if found else None
    m = re.search(r"(?:next|for|over)\s+(?:about\s+|around\s+|roughly\s+|~\s*)?(\d{1,2})\s+days|(\d{1,2})[- ]day",
                  sentence, re.I)
    if m:
        n = int(m.group(1) or m.group(2))
        s = start or carry_start or as_of
        return s, s + timedelta(days=n - 1)
    if re.search(r"\btomorrow\b", sentence, re.I):
        return as_of + timedelta(days=1), None
    return start, None


def heuristic_extract(text: str, catalog: list[dict], suppliers: list[dict], open_pos: list[dict], lots: list[dict],
                      as_of: date) -> dict:
    events = []
    context_skus: list[str] = []
    context_start: date | None = None
    for m in _SENT.finditer(text):
        sent = m.group(0).strip()
        if not sent:
            continue
        skus = _match_skus(sent, catalog, open_pos, lots)
        for sup in suppliers:
            if sup["name"].split()[0].lower() in sent.lower() and not skus:
                skus = [po["sku_id"] for po in open_pos if po["supplier_id"] == sup["supplier_id"]]
        skus = skus or context_skus
        if skus:
            context_skus = skus
        low = sent.lower()
        start, end = _dates(sent, as_of, context_start)
        if start:
            context_start = start
        if re.search(r"delay|delayed|late|postpone|pushed back|held up|slip", low):
            d = re.search(r"(\d{1,2})\s*(?:more\s+|extra\s+|additional\s+)?(?:working\s+)?days?", low)
            if d and skus:
                events.append({"event_type": "supplier_delay", "sku_ids": skus, "magnitude": float(d.group(1)),
                               "start_date": as_of.isoformat(), "end_date": None, "quoted_evidence": sent})
                continue
        if re.search(r"influencer|viral|campaign|promotion|promo|spike|surge|featur|shout-?out|reel|creator|collab"
                     r"|demand|sell(?:ing)? out", low):
            x = re.search(r"(\d+(?:\.\d+)?)\s*(?:x|times)\b", low)
            pct = re.search(r"(\d{1,3})\s*%", low)
            mag = float(x.group(1)) if x else (1 + float(pct.group(1)) / 100 if pct else None)
            if mag and mag > 1 and skus:
                events.append({"event_type": "demand_spike", "sku_ids": skus, "magnitude": mag,
                               "start_date": (start or as_of).isoformat(),
                               "end_date": end.isoformat() if end else None, "quoted_evidence": sent})
                continue
        if re.search(r"drop|decline|fall|cancel", low):
            pct = re.search(r"(\d{1,2})\s*%", low)
            if pct and skus:
                events.append({"event_type": "demand_drop", "sku_ids": skus, "magnitude": 1 - float(pct.group(1)) / 100,
                               "start_date": (start or as_of).isoformat(),
                               "end_date": end.isoformat() if end else None, "quoted_evidence": sent})
                continue
        if re.search(r"expir|best[- ]before|use[- ]by|\bbbd\b|shelf life|short-dated", low) and skus:
            u = re.search(r"(\d[\d,]*)\s*(?:units|pcs|packs|bottles)", low)
            last = end or start
            events.append({"event_type": "expiry_risk", "sku_ids": skus, "magnitude": float(u.group(1).replace(",", "")) if u else 0.0,
                           "start_date": None, "end_date": last.isoformat() if last else None,
                           "quoted_evidence": sent})
    return {"events": events}


def extract(llm: LLMClient | None, text: str, catalog: list[dict], suppliers: list[dict], open_pos: list[dict],
            lots: list[dict], as_of: date):
    """Returns (method, results, call_record). Falls back to the heuristic extractor if the LLM is unavailable or
    returns malformed output; individual invalid events are rejected either way."""
    known = {p["sku_id"] for p in catalog}
    res = run_json_task(
        llm, "signal_extraction", prompts.SIGNAL_SYSTEM,
        prompts.signal_user(text, catalog, suppliers, open_pos, as_of.isoformat()),
        lambda o: validate_extraction(o, text, known), hash_parts=(prompts.PROMPT_VERSION,),
    )
    if res.ok and res.value is not None:
        return "llm", res.value, res.record
    raw = heuristic_extract(text, catalog, suppliers, open_pos, lots, as_of)
    return "heuristic", validate_extraction(raw, text, known), res.record

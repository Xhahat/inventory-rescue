"""Natural-language what-if -> typed ScenarioSpec -> transformed worlds."""

from __future__ import annotations

import re
from dataclasses import replace

from pydantic import BaseModel, Field, field_validator, model_validator

from ..llm import prompts
from ..llm.client import LLMClient, run_json_task
from .simulator import Inbound
from .world import SkuContext, World


class ScenarioSpec(BaseModel):
    demand_multiplier: float = Field(1.0, ge=0.0, le=5.0)
    lead_time_delta_days: int = Field(0, ge=-10, le=60)
    delay_supplier: str | None = None
    supplier_outage: str | None = None
    affected_skus: list[str] = Field(default_factory=list)
    start_day: int = Field(0, ge=0, le=29)
    end_day: int = Field(30, ge=1, le=30)
    budget: float | None = Field(None, ge=0, le=10_000_000)
    summary: str = ""

    @field_validator("affected_skus", mode="before")
    @classmethod
    def _ids(cls, v):
        return [str(x).strip().upper() for x in (v or [])]

    @model_validator(mode="after")
    def _range(self):
        if self.end_day <= self.start_day:
            raise ValueError("end_day must be after start_day")
        return self


def validate_spec(obj: dict, known_skus: set[str], known_suppliers: set[str]) -> ScenarioSpec:
    spec = ScenarioSpec.model_validate(obj)
    bad = [s for s in spec.affected_skus if s not in known_skus]
    if bad:
        raise ValueError(f"unknown SKU(s): {bad}")
    if spec.supplier_outage is not None:
        spec.supplier_outage = spec.supplier_outage.strip().upper()
        if spec.supplier_outage not in known_suppliers:
            raise ValueError(f"unknown supplier: {spec.supplier_outage}")
    if spec.delay_supplier is not None:
        spec.delay_supplier = spec.delay_supplier.strip().upper()
        if spec.delay_supplier not in known_suppliers:
            raise ValueError(f"unknown supplier: {spec.delay_supplier}")
    return spec


def heuristic_parse(text: str, catalog: list[dict], suppliers: list[dict]) -> dict:
    low = text.lower()
    spec: dict = {"summary": text.strip()[:200]}
    m = re.search(r"demand\D{0,30}?(up|rise|rises|increase|increases|jump|jumps|grow|grows|down|drop|drops|fall|falls|"
                  r"decrease|decreases)\D{0,10}?(\d{1,3})\s*%", low) or \
        re.search(r"(\d{1,3})\s*%\s*(more|higher|less|lower)\s+demand", low)
    if m:
        groups = m.groups()
        word, pct = (groups[0], groups[1]) if not groups[0].isdigit() else (groups[1], groups[0])
        sign = -1 if word in ("down", "drop", "drops", "fall", "falls", "decrease", "decreases", "less", "lower") else 1
        spec["demand_multiplier"] = round(1 + sign * int(pct) / 100, 3)
    m = re.search(r"demand\D{0,20}?(\d+(?:\.\d+)?)\s*x", low) or re.search(r"(\d+(?:\.\d+)?)\s*x\s+demand", low)
    if m:
        spec["demand_multiplier"] = float(m.group(1))
    m = re.search(r"demand.{0,40}?\b(doubles?|triples?|halves|halved)\b", low)
    if m:
        spec["demand_multiplier"] = {"d": 2.0, "t": 3.0, "h": 0.5}[m.group(1)[0]]
    m = re.search(r"(\d{1,2})[- ]?(?:extra |more |additional )?days?\s+(?:of\s+)?(?:delay|late|later)", low) or \
        re.search(r"delay(?:ed|s)?\D{0,20}?(\d{1,2})\s*days?", low)
    if m:
        spec["lead_time_delta_days"] = int(m.group(1))
        for s in suppliers:
            if s["supplier_id"].lower() in low or s["name"].split()[0].lower() in low:
                spec["delay_supplier"] = s["supplier_id"]
                break
    if re.search(r"outage|shut|strike|cannot supply|can't supply|unavailable|stops? supplying", low):
        for s in suppliers:
            if s["supplier_id"].lower() in low or s["name"].split()[0].lower() in low:
                spec["supplier_outage"] = s["supplier_id"]
    skus = re.findall(r"sku-\d{3}", low)
    for p in catalog:
        name = p["name"].lower()
        core = " ".join(name.split()[:2])
        if core and core in low:
            skus.append(p["sku_id"].lower())
    cats = {p["category"].lower() for p in catalog}
    for c in cats:
        if c in low or c.rstrip("s") in low:
            skus += [p["sku_id"].lower() for p in catalog if p["category"].lower() == c]
    spec["affected_skus"] = list(dict.fromkeys(s.upper() for s in skus))
    m = re.search(r"(?:budget|inr|₹|rs\.?)\D{0,25}?(\d[\d,]*(?:\.\d+)?)\s*(k|lakh|lac)?\b", low)
    if m:
        v = float(m.group(1).replace(",", "")) * {"k": 1e3, "lakh": 1e5, "lac": 1e5}.get(m.group(2) or "", 1)
        if v >= 1000:
            spec["budget"] = v
    m = re.search(r"(?:next|for)\s+(\d{1,2})\s+days", low)
    if m:
        spec["end_day"] = min(30, max(1, int(m.group(1))))
    m = re.search(r"(?:next|for)\s+(\d|one|two|three|four)\s+weeks?", low)
    if m:
        n = {"one": 1, "two": 2, "three": 3, "four": 4}.get(m.group(1)) or int(m.group(1))
        spec["end_day"] = min(30, max(1, 7 * n))
    return spec


def parse(llm: LLMClient | None, text: str, catalog: list[dict], suppliers: list[dict]):
    known_s = {p["sku_id"] for p in catalog}
    known_sup = {s["supplier_id"] for s in suppliers}
    res = run_json_task(llm, "scenario_parse", prompts.SCENARIO_SYSTEM, prompts.scenario_user(text, catalog, suppliers),
                        lambda o: validate_spec(o, known_s, known_sup), hash_parts=(prompts.PROMPT_VERSION,))
    if res.ok and res.value is not None:
        return "llm", res.value, res.record
    return "heuristic", validate_spec(heuristic_parse(text, catalog, suppliers), known_s, known_sup), res.record


def apply(world: World, ctxs: list[SkuContext], spec: ScenarioSpec) -> World:
    affected = set(spec.affected_skus) or {c.sku_id for c in ctxs}
    by_id = {c.sku_id: c for c in ctxs}
    skus = {}
    for sku_id, sw in world.skus.items():
        ctx = by_id[sku_id]
        hit = sku_id in affected
        paths = sw.demand_paths
        std, exp, inbound = sw.standard_arrival_day, sw.expedite_arrival_day, list(sw.open_inbound)
        if hit and spec.demand_multiplier != 1.0:
            new_paths = []
            for w, p in paths:
                q = p.copy()
                q[spec.start_day:spec.end_day] *= spec.demand_multiplier
                new_paths.append((w, q))
            paths = new_paths
        if spec.lead_time_delta_days and (ctx.supplier_id == spec.delay_supplier if spec.delay_supplier else hit):
            d = spec.lead_time_delta_days
            std = None if std is None else max(0, std + d)
            inbound = [Inbound(i.ref_id, i.qty, None if i.arrival_day is None else max(0, i.arrival_day + d))
                       for i in inbound]
        if spec.supplier_outage and ctx.supplier_id == spec.supplier_outage:
            std = exp = None
            inbound = [Inbound(i.ref_id, i.qty, None if i.arrival_day is None or
                               spec.start_day <= i.arrival_day < spec.end_day else i.arrival_day) for i in inbound]
        skus[sku_id] = replace(sw, demand_paths=paths, standard_arrival_day=std, expedite_arrival_day=exp,
                               open_inbound=inbound)
    return World(f"{world.name}+scenario", skus, {**world.description, "scenario": spec.model_dump()})

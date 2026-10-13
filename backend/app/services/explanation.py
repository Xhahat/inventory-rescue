"""Plan explanation: narrative only from computed plan JSON; cites record ids; never introduces new numbers."""

from __future__ import annotations

import re

from pydantic import BaseModel, Field

from ..llm import prompts
from ..llm.client import LLMClient, run_json_task

_ID = re.compile(r"\b[A-Z]{2,6}-[A-Z0-9]+(?:-[A-Z0-9]+)*\b")
_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_NUM = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?")


class Bullet(BaseModel):
    text: str = Field(max_length=600)
    cites: list[str] = Field(default_factory=list)


class Explanation(BaseModel):
    summary: str = Field(max_length=1200)
    bullets: list[Bullet] = Field(default_factory=list, max_length=8)


def _walk(o, nums: set[float], strs: set[str]) -> None:
    if isinstance(o, bool):
        return
    if isinstance(o, (int, float)):
        nums.add(float(o))
    elif isinstance(o, str):
        strs.add(o)
        for m in _NUM.finditer(_DATE.sub(" ", _ID.sub(" ", o))):
            nums.add(float(m.group(0).replace(",", "")))
    elif isinstance(o, dict):
        for k, v in o.items():
            _walk(v, nums, strs)
    elif isinstance(o, (list, tuple)):
        for v in o:
            _walk(v, nums, strs)


def allowed_values(plan_json: dict) -> tuple[set[float], set[str], set[str]]:
    nums: set[float] = set()
    strs: set[str] = set()
    _walk(plan_json, nums, strs)
    allowed = set()
    for v in nums:
        allowed |= {round(v, 2), round(v, 1), float(round(v)), round(v * 100, 1), float(round(v * 100))}
        if abs(v) >= 1000:  # "12.5k" / "1.2 lakh" style is not allowed, but thousands rounding is
            allowed.add(float(round(v, -2)))
    ids = set()
    dates = set()
    for s in strs:
        ids |= set(_ID.findall(s))
        dates |= set(_DATE.findall(s))
    return allowed, ids, dates


def validate_explanation(obj: dict, plan_json: dict) -> Explanation:
    exp = Explanation.model_validate(obj)
    allowed, ids, dates = allowed_values(plan_json)
    texts = [exp.summary] + [b.text for b in exp.bullets]
    for t in texts:
        for d in _DATE.findall(t):
            if d not in dates:
                raise ValueError(f"date {d} not in plan")
        cleaned = _DATE.sub(" ", _ID.sub(" ", t))
        for m in _NUM.finditer(cleaned):
            x = float(m.group(0).replace(",", ""))
            if not any(abs(x - a) < 0.011 for a in allowed):
                raise ValueError(f"number {m.group(0)} not present in plan JSON")
    for b in exp.bullets:
        kept = []
        for c in b.cites:
            c = c.strip()
            if c in ids:
                kept.append(c)
            elif _ID.fullmatch(c):
                raise ValueError(f"unknown citation {c}")
            # anything else (e.g. a JSON field name) is not a record claim: drop it
        b.cites = kept
    return exp


def template_explanation(plan_json: dict) -> Explanation:
    acts = plan_json.get("actions", [])
    summary = (f"This plan uses INR {plan_json['cash_used']:,.0f} of the INR {plan_json['budget']:,.0f} budget across "
               f"{len(acts)} actions. Simulated expected loss falls from INR {plan_json['expected_loss_no_action']:,.0f} "
               f"(no action) to INR {plan_json['expected_loss_plan']:,.0f}.")
    bullets = []
    for a in acts[:6]:
        what = a["kind"].replace("_", " ").replace("+", " + ")
        qty = f" {a['order_qty']} units" if a.get("order_qty") else ""
        if a.get("later_qty"):
            qty += f" now + {a['later_qty']} on {a.get('later_order_date')}"
        md = f" at {a['discount_pct'] * 100:.0f}% off" if a.get("discount_pct") else ""
        reason = a["reasons"][0] if a.get("reasons") else ""
        bullets.append(Bullet(
            text=f"{a['id']}: {what}{qty}{md} for {a['sku_name']} (cash INR {a['cash_outlay']:,.0f}, "
                 f"avoids INR {a['loss_avoided']:,.0f} expected loss). {reason}".strip(),
            cites=[a["id"]] + list(a.get("record_ids", []))[:4]))
    return Explanation(summary=summary, bullets=bullets)


def explain(llm: LLMClient | None, plan_json: dict):
    res = run_json_task(llm, "plan_explanation", prompts.EXPLAIN_SYSTEM, prompts.explain_user(plan_json),
                        lambda o: validate_explanation(o, plan_json), hash_parts=(prompts.PROMPT_VERSION,),
                        repair_attempts=1)
    if res.ok and res.value is not None:
        return "llm", res.value, res.record, None
    return "template", template_explanation(plan_json), res.record, res.error

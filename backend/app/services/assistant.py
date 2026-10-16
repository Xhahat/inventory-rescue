"""Planning assistant: a tool-calling loop over the deterministic planner, with a grounding check. Every number,
date and record id in the final answer must appear in a tool result (or the user's message); otherwise the model
gets one corrective retry, then the tool results are returned with a deterministic summary."""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from ..llm import assistant_prompts as ap
from ..llm.agent import ToolCall, ToolSpec, make_tool_session
from ..llm.client import CallRecord, get_llm, input_hash, persist_records
from . import assistant_tools as at
from . import explanation as expl

log = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 6
MAX_MODEL_CALLS = 9
MAX_CALLS_PER_ROUND = 6
MAX_HISTORY = 12
MAX_MESSAGE_CHARS = 4000
DEADLINE_SECONDS = 150.0
SMALL_INT = 10  # bare counts up to this ("3 SKUs", "step 2") need no source

DISABLED_MESSAGE = ("The planning assistant needs the model, and the LLM is disabled or not configured on this server. "
                    "The plan, SKU and scenario pages still work with deterministic fallbacks.")

# ---------------------------------------------------------------- grounding check

_REF_ID = re.compile(r"\b(?:SKU|SUP|LOT|PO|DPO|EV|ACT|FC|CTX)-[A-Z0-9]+(?:-[A-Z0-9]+)*\b")
_MONTH = (r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sept?(?:ember)?|"
          r"oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)")
_DM = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+" + _MONTH + r"\b\.?(?:,?\s+(\d{4}))?", re.I)
_MD = re.compile(r"\b" + _MONTH + r"\b\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b(?:,?\s+(\d{4}))?", re.I)
_TOKEN = re.compile(r"(?<![\w.])(\d[\d,]*(?:\.\d+)?)(?:\s*(k|lakhs?|lacs?|crores?|cr)\b)?(\s*%)?", re.I)
_CURRENCY = re.compile(r"(?:₹|inr|rs\.?)\s*[~≈]?\s*$", re.I)
_UNIT = {"k": 1e3, "lakh": 1e5, "lakhs": 1e5, "lac": 1e5, "lacs": 1e5, "cr": 1e7, "crore": 1e7, "crores": 1e7}
_MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov",
                                       "dec"), 1)}


@dataclass
class Grounding:
    ok: bool
    checked: int
    unsupported: list[str] = field(default_factory=list)


def _user_numbers(texts: list[str]) -> list[float]:
    out = []
    for t in texts:
        for m in _TOKEN.finditer(expl._DATE.sub(" ", expl._ID.sub(" ", t))):
            v = float(m.group(1).replace(",", "")) * _UNIT.get((m.group(2) or "").lower(), 1)
            out.append(v)
            if m.group(3):
                out.append(v / 100)
    return out


def check_grounding(text: str, evidence: list, user_texts: list[str] = ()) -> Grounding:
    """Numbers (incl. k / lakh / crore and %), ISO and natural-language dates and record ids in `text` must come
    from `evidence` (tool results/args) or the user's messages. Rounding as in the plan explanation is allowed."""
    ev = list(evidence) + [{"user": list(user_texts)}]
    raw: set[float] = set()
    strs: set[str] = set()
    expl._walk(ev, raw, strs)
    raw |= set(_user_numbers(list(user_texts)))
    allowed, _, dates = expl.allowed_values({"v": sorted(raw)})
    for s in strs:
        dates |= set(expl._DATE.findall(s))
    allowed |= {abs(a) for a in allowed} | {round(v * 100, 2) for v in raw}
    allowed |= {float(d[:4]) for d in dates}
    ids: set[str] = set()
    for s in strs:
        ids |= set(_REF_ID.findall(s.upper()))
    md = {(int(d[5:7]), int(d[8:10])) for d in dates}
    absraw = [abs(v) for v in raw]
    bad: list[str] = []
    checked = 0

    for i in _REF_ID.findall(text):
        checked += 1
        if i not in ids:
            bad.append(i)
    for d in expl._DATE.findall(text):
        checked += 1
        if d not in dates:
            bad.append(d)
    t = expl._DATE.sub(" ", text)

    def nat(m: re.Match, day_first: bool) -> str:
        nonlocal checked
        day, mon = (m.group(1), m.group(2)) if day_first else (m.group(2), m.group(1))
        checked += 1
        if (_MONTHS[mon[:3].lower()], int(day)) not in md:
            bad.append(m.group(0).strip())
        return " "

    t = _DM.sub(lambda m: nat(m, True), t)
    t = _MD.sub(lambda m: nat(m, False), t)
    t = expl._ID.sub(" ", _REF_ID.sub(" ", t))
    for m in _TOKEN.finditer(t):
        num, unit, pct = m.group(1), (m.group(2) or "").lower(), bool(m.group(3))
        x = float(num.replace(",", ""))
        checked += 1
        if unit:
            mult = _UNIT[unit]
            decimals = len(num.split(".")[1]) if "." in num else 0
            tol = 0.5 * 10 ** (-decimals) * mult + 0.01
            if any(abs(v - x * mult) <= tol for v in absraw):
                continue
        elif any(abs(x - a) < 0.011 for a in allowed):
            continue
        elif not pct and "." not in num and any(0 <= v - x < 1 for v in absraw):
            continue  # whole number written by cutting off a tool value's decimals (2,847,836.6 -> 2,847,836)
        elif not pct and x.is_integer() and x <= SMALL_INT and not _CURRENCY.search(t[:m.start()]):
            continue
        bad.append(m.group(0).strip())
    return Grounding(not bad, checked, list(dict.fromkeys(bad)))


# ---------------------------------------------------------------- loop

def clean_history(messages: list[dict]) -> list[dict]:
    hist = [{"role": m.get("role"), "content": str(m.get("content") or "")[:MAX_MESSAGE_CHARS]}
            for m in messages if m.get("role") in ("user", "assistant") and str(m.get("content") or "").strip()]
    hist = hist[-MAX_HISTORY:]
    while hist and hist[0]["role"] != "user":
        hist.pop(0)
    if not hist or hist[-1]["role"] != "user":
        raise ValueError("the last message must be a non-empty user message")
    return hist


def _dispatch(tools: dict[str, ToolSpec], call: ToolCall) -> dict:
    spec = tools.get(call.name)
    if spec is None:
        return {"error": f"unknown tool {call.name!r}; available: {sorted(tools)}"}
    if call.arg_error:
        return {"error": call.arg_error}
    allowed = set((spec.parameters.get("properties") or {}))
    extra = sorted(set(call.args) - allowed)
    if extra:
        return {"error": f"unknown argument(s) {extra}; allowed: {sorted(allowed)}"}
    missing = [k for k in spec.parameters.get("required", []) if call.args.get(k) in (None, "")]
    if missing:
        return {"error": f"missing required argument(s) {missing}"}
    try:
        return spec.fn(**{k: v for k, v in call.args.items() if v is not None})
    except at.ToolError as exc:
        return {"error": str(exc)}
    except Exception as exc:  # a tool bug must not break the chat; the model sees a short error
        log.exception("assistant tool %s failed", call.name)
        return {"error": f"tool failed: {type(exc).__name__}"}


def _fallback_answer(reason: str, calls: list[dict]) -> str:
    lines = [f"**Fallback (deterministic summary).** The model's answer could not be verified against the tool "
             f"results ({reason}), so these are the tool results themselves:"]
    lines += [f"- `{c['name']}`: {c['result_summary']}" for c in calls]
    return "\n".join(lines)


# Stray C0 control characters occasionally appear in model output (seen with Gemini), as does a stray leading
# punctuation mark (".Roasted Makhana ...").
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_LEADING_PUNCT = re.compile(r"^[.,;:]+\s*(?=[A-Za-z0-9*#₹])")

def answer(db: Session, messages: list[dict], llm=None, style: str = "default") -> dict:
    t0 = time.perf_counter()
    hist = clean_history(messages)
    llm = llm if llm is not None else get_llm()
    base = {"tool_calls": [], "validated": False, "numbers_checked": 0, "unsupported": [], "rounds": 0}
    tc = at.ToolContext(db)
    tools = at.build_tools(tc)
    by_name = {t.name: t for t in tools}
    session = make_tool_session(llm, ap.system_for(style), hist, tools)
    if session is None:
        msg = DISABLED_MESSAGE if llm is None else \
            f"The configured model client ({type(llm).__name__}) does not support tool calling."
        return {**base, "answer": msg, "method": "disabled", "llm_disabled": True, "model": None, "latency_ms": 0}

    user_texts = [m["content"] for m in hist if m["role"] == "user"]
    h = input_hash("assistant", ap.ASSISTANT_PROMPT_VERSION, style, hist)
    records: list[CallRecord] = []
    calls_log: list[dict] = []
    evidence: list = []
    rounds = model_calls = 0
    repaired = False
    reason = "the model did not produce an answer"
    grounding = Grounding(False, 0)

    while model_calls < MAX_MODEL_CALLS:
        if time.perf_counter() - t0 > DEADLINE_SECONDS:
            reason = "time limit reached"
            break
        t1 = time.perf_counter()
        model_calls += 1
        try:
            turn = session.step(allow_tools=rounds < MAX_TOOL_ROUNDS)
        except Exception as exc:  # provider / network errors
            err = f"{type(exc).__name__}: {str(exc)[:400]}"
            records.append(CallRecord("assistant", h, llm.model, "error", err, int((time.perf_counter() - t1) * 1000),
                                      None))
            reason = f"model error: {type(exc).__name__}"
            break
        ms = int((time.perf_counter() - t1) * 1000)
        if turn.calls:
            rounds += 1
            records.append(CallRecord("assistant", h, llm.model, "tool_calls", None, ms,
                                      json.dumps([{"name": c.name, "args": c.args} for c in turn.calls])[:20000]))
            results = []
            for i, c in enumerate(turn.calls):
                res = _dispatch(by_name, c) if i < MAX_CALLS_PER_ROUND else \
                    {"error": f"at most {MAX_CALLS_PER_ROUND} tool calls per round"}
                results.append((c, res))
                if "error" not in res:
                    evidence.append({"args": c.args, "result": res})
                calls_log.append({"name": c.name, "args": c.args, "ok": "error" not in res,
                                  "result_summary": at.summarize(c.name, res), "result": res})
            session.add_tool_results(results)
            continue
        text = _LEADING_PUNCT.sub("", _CONTROL_CHARS.sub("", turn.text or "").strip())
        if not text:
            records.append(CallRecord("assistant", h, llm.model, "invalid", "empty answer", ms, None))
            reason = "empty answer"
            break
        grounding = check_grounding(text, evidence, user_texts)
        if grounding.ok:
            records.append(CallRecord("assistant", h, llm.model, "ok_repaired" if repaired else "ok", None, ms,
                                      text[:20000]))
            persist_records(db, records)
            db.commit()
            return {**base, "answer": text, "tool_calls": calls_log, "validated": True,
                    "method": "llm_repaired" if repaired else "llm", "numbers_checked": grounding.checked,
                    "rounds": rounds, "llm_disabled": False, "model": llm.model,
                    "latency_ms": int((time.perf_counter() - t0) * 1000)}
        err = f"unsupported values: {', '.join(grounding.unsupported[:12])}"
        records.append(CallRecord("assistant", h, llm.model, "invalid", err, ms, text[:20000]))
        if repaired:
            reason = err
            break
        repaired = True
        session.add_user(ap.correction(grounding.unsupported))

    if not calls_log:
        res = _dispatch(by_name, ToolCall("fallback", "get_plan_summary", {}))
        calls_log.append({"name": "get_plan_summary", "args": {}, "ok": "error" not in res,
                          "result_summary": at.summarize("get_plan_summary", res), "result": res})
    persist_records(db, records)
    db.commit()
    return {**base, "answer": _fallback_answer(reason, calls_log), "tool_calls": calls_log, "method": "fallback",
            "numbers_checked": grounding.checked, "unsupported": grounding.unsupported, "rounds": rounds,
            "llm_disabled": False, "model": llm.model, "latency_ms": int((time.perf_counter() - t0) * 1000)}

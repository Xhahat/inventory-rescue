"""LLM adapter behind the `LLMClient` protocol. Google Cloud: Gemini on Vertex AI (google-genai SDK, Application
Default Credentials, i.e. the Cloud Run service account). Local development: any OpenAI-compatible endpoint
(e.g. a LiteLLM proxy). All callers go through `run_json_task`, which hashes inputs, parses JSON, validates and
returns a log record (callers persist it)."""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Generic, Protocol, TypeVar

from ..config import get_settings

T = TypeVar("T")


class LLMClient(Protocol):
    model: str

    def complete(self, system: str, user: str) -> str: ...


class LiteLLMClient:
    def __init__(self, base_url: str, api_key: str, model: str, timeout: float = 60.0):
        from openai import OpenAI

        self.model = model
        self._client = OpenAI(base_url=base_url, api_key=api_key, timeout=timeout, max_retries=1)
        self._json_mode = True

    def complete(self, system: str, user: str) -> str:
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        kwargs: dict[str, Any] = {"model": self.model, "messages": messages, "temperature": 0}
        if self._json_mode:
            try:
                resp = self._client.chat.completions.create(response_format={"type": "json_object"}, **kwargs)
                return resp.choices[0].message.content or ""
            except Exception as exc:  # some backends reject response_format / temperature
                if "response_format" not in str(exc) and "json" not in str(exc).lower():
                    raise
                self._json_mode = False
        resp = self._client.chat.completions.create(**kwargs)
        return resp.choices[0].message.content or ""


class VertexGeminiClient:
    """Gemini on Vertex AI. JSON output is requested natively (response_mime_type) at temperature 0; the same
    validators as every other provider still decide whether the output is used."""

    def __init__(self, project: str, location: str, model: str, timeout: float = 60.0, client: Any = None):
        from google.genai import types

        self.model = model
        self._types = types
        if client is None:
            from google import genai

            client = genai.Client(vertexai=True, project=project, location=location,
                                  http_options=types.HttpOptions(timeout=int(timeout * 1000)))
        self._client = client

    def complete(self, system: str, user: str) -> str:
        cfg = self._types.GenerateContentConfig(system_instruction=system, temperature=0,
                                                response_mime_type="application/json")
        resp = self._client.models.generate_content(model=self.model, contents=user, config=cfg)
        return resp.text or ""


def build_llm(s) -> LLMClient:
    if s.resolved_llm_provider == "vertex":
        return VertexGeminiClient(s.gcp_project, s.gcp_location, s.vertex_model, s.llm_timeout_seconds)
    return LiteLLMClient(s.litellm_base_url, s.litellm_api_key, s.llm_model, s.llm_timeout_seconds)


_override: LLMClient | None = None
_override_set = False
_lock = threading.Lock()
_cached: LLMClient | None = None


def set_llm_override(client: LLMClient | None) -> None:
    """Tests inject a fake client (or None to simulate 'LLM unavailable')."""
    global _override, _override_set
    _override, _override_set = client, True


def clear_llm_override() -> None:
    global _override, _override_set
    _override, _override_set = None, False


def get_llm() -> LLMClient | None:
    global _cached
    if _override_set:
        return _override
    s = get_settings()
    if not s.llm_configured:
        return None
    with _lock:
        if _cached is None:
            _cached = build_llm(s)
    return _cached


def input_hash(*parts: Any) -> str:
    blob = json.dumps(parts, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def parse_json_object(text: str) -> dict:
    t = _FENCE.sub("", text.strip()).strip()
    try:
        obj = json.loads(t)
    except json.JSONDecodeError:
        start, end = t.find("{"), t.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("no JSON object in response")
        obj = json.loads(t[start : end + 1])
    if not isinstance(obj, dict):
        raise ValueError("response is not a JSON object")
    return obj


@dataclass
class CallRecord:
    task: str
    input_hash: str
    model: str
    status: str  # ok | invalid | error | unavailable
    error: str | None
    latency_ms: int
    output: str | None


@dataclass
class TaskResult(Generic[T]):
    ok: bool
    value: T | None
    error: str | None
    record: CallRecord


def run_json_task(llm: LLMClient | None, task: str, system: str, user: str,
                  validate: Callable[[dict], T], hash_parts: tuple = (),
                  repair_attempts: int = 0) -> TaskResult[T]:
    """Call the LLM, parse JSON and validate. With repair_attempts>0, a validation failure is fed back to the
    model once per attempt; the final output must still pass the same validator."""
    h = input_hash(task, system, user, *hash_parts)
    if llm is None:
        rec = CallRecord(task, h, "none", "unavailable", "LLM not configured", 0, None)
        return TaskResult(False, None, "LLM not configured", rec)
    t0 = time.perf_counter()
    raw: str | None = None
    prompt = user
    try:
        for attempt in range(repair_attempts + 1):
            raw = llm.complete(system, prompt)
            try:
                value = validate(parse_json_object(raw))
                break
            except (ValueError, TypeError, KeyError) as exc:
                if attempt >= repair_attempts:
                    raise
                prompt = (f"{user}\n\nYour previous answer was rejected by the validator: {str(exc)[:300]}\n"
                          "Return a corrected JSON object. Use only numbers, dates and ids that appear verbatim "
                          "in the input; do not compute new totals or differences.")
        status = "ok" if attempt == 0 else "ok_repaired"
        rec = CallRecord(task, h, llm.model, status, None, int((time.perf_counter() - t0) * 1000), raw[:20000])
        return TaskResult(True, value, None, rec)
    except (ValueError, TypeError, KeyError) as exc:
        msg = f"invalid output: {str(exc)[:500]}"
        rec = CallRecord(task, h, llm.model, "invalid", msg, int((time.perf_counter() - t0) * 1000),
                         raw[:20000] if raw else None)
        return TaskResult(False, None, msg, rec)
    except Exception as exc:  # network / provider errors
        msg = f"{type(exc).__name__}: {str(exc)[:500]}"
        rec = CallRecord(task, h, getattr(llm, "model", "?"), "error", msg, int((time.perf_counter() - t0) * 1000), None)
        return TaskResult(False, None, msg, rec)


def persist_records(db, records: list[CallRecord]) -> None:
    from ..models import LLMCall

    for r in records:
        db.add(LLMCall(task=r.task, input_hash=r.input_hash, model=r.model, status=r.status, error=r.error,
                       latency_ms=r.latency_ms, output=r.output))

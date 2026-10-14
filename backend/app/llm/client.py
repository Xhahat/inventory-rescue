"""LLM adapter behind the `LLMClient` protocol. Default: Gemini through the google-genai SDK, either the Gemini API
with a Google AI Studio key or Vertex AI with Application Default Credentials (the Cloud Run service account).
All callers go through `run_json_task`, which hashes inputs, parses JSON, validates and returns a log record (callers persist it)."""

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


class _RateLimiter:
    """Spaces calls evenly so at most `rpm` requests start per minute (0 = unlimited), across threads."""

    def __init__(self, rpm: int):
        self.interval = 60.0 / rpm if rpm and rpm > 0 else 0.0
        self._next = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        if not self.interval:
            return
        with self._lock:
            now = time.monotonic()
            start = max(now, self._next)
            self._next = start + self.interval
        if start > now:
            time.sleep(start - now)


class VertexGeminiClient:
    """Gemini via the google-genai SDK: Vertex AI (Application Default Credentials) by default, or the Gemini API
    with a Google AI Studio key (see `GeminiAPIClient`). JSON output is requested natively (response_mime_type);
    the same validators still decide whether the output is used."""

    def __init__(self, project: str | None, location: str, model: str, timeout: float = 60.0, client: Any = None,
                 thinking_level: str | None = None, api_key: str | None = None, rpm: int = 0):
        from google.genai import types

        self.model = model
        self._types = types
        self.thinking_level = (thinking_level or "").strip().upper() or None
        self._limiter = _RateLimiter(rpm)
        if client is None:
            from google import genai

            # Retry rate limits (429; per-minute quotas reset within a minute) and transient overload (5xx).
            retry = types.HttpRetryOptions(attempts=6, initial_delay=4.0, max_delay=60.0,
                                           http_status_codes=[408, 429, 500, 502, 503, 504])
            http = types.HttpOptions(timeout=int(timeout * 1000), retry_options=retry)
            if api_key:
                client = genai.Client(api_key=api_key, http_options=http)
            else:
                client = genai.Client(vertexai=True, project=project, location=location, http_options=http)
        self._client = client

    def config(self, **kw) -> Any:
        """GenerateContentConfig with the shared settings (temperature 0, optional thinking level)."""
        if self.thinking_level:
            kw["thinking_config"] = self._types.ThinkingConfig(thinking_level=self.thinking_level)
        return self._types.GenerateContentConfig(temperature=0, **kw)

    def generate(self, contents: Any, config: Any) -> Any:
        self._limiter.wait()
        return self._client.models.generate_content(model=self.model, contents=contents, config=config)

    def complete(self, system: str, user: str) -> str:
        cfg = self.config(system_instruction=system, response_mime_type="application/json")
        return self.generate(user, cfg).text or ""


class GeminiAPIClient(VertexGeminiClient):
    """Gemini API (generativelanguage.googleapis.com) authenticated with a Google AI Studio API key."""

    def __init__(self, api_key: str, model: str, timeout: float = 60.0, client: Any = None,
                 thinking_level: str | None = None, rpm: int = 0):
        super().__init__(None, "global", model, timeout, client, thinking_level, api_key=api_key, rpm=rpm)


def build_llm(s) -> LLMClient:
    p = s.resolved_llm_provider
    if p == "gemini":
        return GeminiAPIClient(s.gemini_api_key, s.gemini_model, s.llm_timeout_seconds,
                               thinking_level=s.gemini_thinking_level, rpm=s.gemini_rpm)
    return VertexGeminiClient(s.gcp_project, s.gcp_location, s.gemini_model, s.llm_timeout_seconds,
                              thinking_level=s.gemini_thinking_level, rpm=s.gemini_rpm)


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

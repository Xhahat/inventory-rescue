"""Optional live Gemini smoke test. Skipped unless a Gemini API key (or Vertex AI project) is configured
(env vars or env/.env) and IRC_LIVE_LLM=1."""

import os

import pytest

from app.config import Settings
from app.llm.client import build_llm, parse_json_object


def _settings():
    s = Settings()
    if os.environ.get("IRC_LIVE_LLM") != "1" or not s.llm_configured:
        pytest.skip("live LLM test disabled (set IRC_LIVE_LLM=1 with a Gemini API key)")
    return s


def test_live_gemini_returns_json():
    c = build_llm(_settings())
    out = parse_json_object(c.complete("Respond with a single JSON object only.", 'Return {"ok": true}.'))
    assert out.get("ok") is True

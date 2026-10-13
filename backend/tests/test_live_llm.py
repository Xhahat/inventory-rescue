"""Optional live LiteLLM smoke test. Skipped unless LITELLM_BASE_URL/LITELLM_API_KEY are configured
(env vars or env/.env) and IRC_LIVE_LLM=1."""

import os

import pytest

from app.config import Settings
from app.llm.client import LiteLLMClient, parse_json_object


def _settings():
    s = Settings()
    if os.environ.get("IRC_LIVE_LLM") != "1" or not (s.litellm_base_url and s.litellm_api_key):
        pytest.skip("live LLM test disabled (set IRC_LIVE_LLM=1 with LiteLLM credentials)")
    return s


def test_live_litellm_returns_json():
    s = _settings()
    c = LiteLLMClient(s.litellm_base_url, s.litellm_api_key, s.llm_model, s.llm_timeout_seconds)
    out = parse_json_object(c.complete("Respond with a single JSON object only.", 'Return {"ok": true}.'))
    assert out.get("ok") is True

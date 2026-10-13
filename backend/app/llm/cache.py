"""Response cache for batch evaluations (backtest, extraction eval). Calls run at temperature 0, so the raw response
is cached by input hash (model + system + user prompt) in storage (var/cache/llm locally, Cloud Storage on Google
Cloud). Validators still run on every (cached or fresh) response."""

from __future__ import annotations

import json
import threading

from ..adapters.storage import get_storage
from .client import LiteLLMClient, LLMClient, VertexGeminiClient, input_hash


class CachedLLM:
    def __init__(self, inner: LLMClient, namespace: str = "llm"):
        self.inner = inner
        self.model = inner.model
        self.namespace = namespace
        self.hits = 0
        self.misses = 0
        self._lock = threading.Lock()

    def _key(self, system: str, user: str) -> str:
        return f"cache/{self.namespace}/{input_hash(self.model, system, user)}.json"

    def complete(self, system: str, user: str) -> str:
        key = self._key(system, user)
        try:
            st = get_storage()
            if st.exists(key):
                text = json.loads(st.get(key))["text"]
                with self._lock:
                    self.hits += 1
                return text
        except Exception:  # cache only: a broken entry or storage error means a fresh call
            pass
        text = self.inner.complete(system, user)
        with self._lock:
            self.misses += 1
        try:
            get_storage().put(key, json.dumps({"model": self.model, "text": text}).encode("utf-8"))
        except Exception:  # cache only
            pass
        return text


def cached(llm: LLMClient | None, namespace: str = "llm") -> LLMClient | None:
    """Wrap real provider clients only; injected test doubles stay uncached so each test sees its own handler."""
    if isinstance(llm, (LiteLLMClient, VertexGeminiClient)):
        return CachedLLM(llm, namespace)
    return llm

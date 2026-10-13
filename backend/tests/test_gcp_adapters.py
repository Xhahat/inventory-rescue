"""Google Cloud adapters (Gemini on Vertex AI, Cloud Storage, single-service static hosting) with fake SDK clients,
so no credentials or network are needed."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.adapters import storage as storage_mod
from app.config import Settings, get_settings
from app.llm import client as llm_client


def _settings(**env) -> Settings:
    return Settings(_env_file=None, **env)


def test_provider_resolution(monkeypatch):
    for k in ("GCP_PROJECT", "GOOGLE_CLOUD_PROJECT", "LLM_PROVIDER", "LLM_DISABLED"):
        monkeypatch.delenv(k, raising=False)
    s = _settings(LITELLM_BASE_URL="http://x", LITELLM_API_KEY="k")
    assert s.resolved_llm_provider == "openai_compatible" and s.llm_configured

    s = _settings(GOOGLE_CLOUD_PROJECT="demo-proj")
    assert s.resolved_llm_provider == "vertex" and s.llm_configured
    assert s.active_llm_model == "gemini-2.5-flash" and s.gcp_location == "global"

    s = _settings(GOOGLE_CLOUD_PROJECT="demo-proj", llm_provider="openai_compatible")
    assert s.resolved_llm_provider == "openai_compatible" and not s.llm_configured

    s = _settings(llm_provider="vertex")
    assert not s.llm_configured  # vertex needs a project
    assert not _settings(GOOGLE_CLOUD_PROJECT="p", llm_disabled=True).llm_configured


class _FakeModels:
    def __init__(self, text: str):
        self.text = text
        self.calls: list[dict] = []

    def generate_content(self, model, contents, config):
        self.calls.append({"model": model, "contents": contents, "config": config})
        return type("Resp", (), {"text": self.text})()


class _FakeGenai:
    def __init__(self, text: str):
        self.models = _FakeModels(text)


def test_vertex_client_requests_json_and_feeds_validator():
    fake = _FakeGenai('{"answer": 42}')
    c = llm_client.VertexGeminiClient("p", "global", "gemini-2.5-flash", client=fake)
    res = llm_client.run_json_task(c, "t", "SYSTEM", "USER", lambda d: d["answer"])
    assert res.ok and res.value == 42 and res.record.model == "gemini-2.5-flash"
    call = fake.models.calls[0]
    assert call["model"] == "gemini-2.5-flash" and call["contents"] == "USER"
    cfg = call["config"]
    assert cfg.system_instruction == "SYSTEM" and cfg.temperature == 0
    assert cfg.response_mime_type == "application/json"

    bad = llm_client.VertexGeminiClient("p", "global", "gemini-2.5-flash", client=_FakeGenai("not json"))
    res = llm_client.run_json_task(bad, "t", "S", "U", lambda d: d["answer"])
    assert not res.ok and res.record.status == "invalid"


def test_build_llm_picks_vertex(monkeypatch):
    import google.genai as genai

    made = {}

    class _Client:
        def __init__(self, **kw):
            made.update(kw)
            self.models = _FakeModels("{}")

    monkeypatch.setattr(genai, "Client", _Client)
    c = llm_client.build_llm(_settings(GOOGLE_CLOUD_PROJECT="demo-proj", GCP_LOCATION="europe-west4"))
    assert isinstance(c, llm_client.VertexGeminiClient)
    assert made["vertexai"] is True and made["project"] == "demo-proj" and made["location"] == "europe-west4"


class _Blob:
    def __init__(self, store, name):
        self.store, self.name = store, name

    def upload_from_string(self, data):
        self.store[self.name] = bytes(data)

    def download_as_bytes(self):
        return self.store[self.name]

    def exists(self):
        return self.name in self.store


class _Bucket:
    def __init__(self, store):
        self.store = store

    def blob(self, name):
        return _Blob(self.store, name)


class _GcsClient:
    def __init__(self):
        self.store: dict[str, bytes] = {}
        self.buckets: list[str] = []

    def bucket(self, name):
        self.buckets.append(name)
        return _Bucket(self.store)


def test_gcs_storage_roundtrip_and_key_validation():
    gc = _GcsClient()
    st = storage_mod.GCSStorage("my-bucket", "/irc/", client=gc)
    assert gc.buckets == ["my-bucket"]
    assert st.put("exports/a.csv", b"x,y") == "exports/a.csv"
    assert gc.store == {"irc/exports/a.csv": b"x,y"}
    assert st.exists("exports/a.csv") and not st.exists("exports/b.csv")
    assert st.get("exports/a.csv") == b"x,y"
    for bad in ("../secret", "a/../../b", "/abs", "sp ace", "a\\b"):
        with pytest.raises(ValueError):
            st.put(bad, b"")


def test_get_storage_selects_backend(monkeypatch):
    monkeypatch.setenv("STORAGE_BACKEND", "gcs")
    monkeypatch.delenv("GCS_BUCKET", raising=False)
    get_settings.cache_clear()
    try:
        with pytest.raises(RuntimeError):
            storage_mod.get_storage()
        monkeypatch.setenv("GCS_BUCKET", "b1")
        get_settings.cache_clear()
        monkeypatch.setattr(storage_mod, "GCSStorage", lambda bucket, prefix: ("gcs", bucket, prefix))
        storage_mod._gcs.cache_clear()
        assert storage_mod.get_storage() == ("gcs", "b1", "")
    finally:
        storage_mod._gcs.cache_clear()
        monkeypatch.delenv("STORAGE_BACKEND")
        get_settings.cache_clear()
    assert isinstance(storage_mod.get_storage(), storage_mod.LocalStorage)


def test_serves_built_frontend_next_to_api(monkeypatch, tmp_path, seeded_db):
    (tmp_path / "index.html").write_text("<html>inventory rescue</html>", encoding="utf-8")
    monkeypatch.setenv("STATIC_DIR", str(tmp_path))
    get_settings.cache_clear()
    try:
        from app.main import create_app

        c = TestClient(create_app())
        r = c.get("/")
        assert r.status_code == 200 and "inventory rescue" in r.text
        assert c.get("/health").json()["status"] == "ok"
        assert c.get("/api/overview").status_code == 200
        assert c.get("/api/does-not-exist").status_code == 404
    finally:
        monkeypatch.delenv("STATIC_DIR")
        get_settings.cache_clear()

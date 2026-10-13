from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(str(ROOT_DIR / ".env"), str(ROOT_DIR / "env" / ".env")),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    litellm_base_url: str | None = Field(default=None, validation_alias=AliasChoices(
        "LITELLM_BASE_URL", "LITELLM_URL", "LITELLM_API_BASE", "LLM_BASE_URL", "OPENAI_BASE_URL", "OPENAI_API_BASE",
        "API_BASE_URL", "BASE_URL"))
    litellm_api_key: str | None = Field(default=None, validation_alias=AliasChoices(
        "LITELLM_API_KEY", "LITELLM_KEY", "LLM_API_KEY", "OPENAI_API_KEY", "API_KEY"))
    llm_model: str = Field(default="gpt-5.4-mini", validation_alias=AliasChoices(
        "LLM_MODEL", "LITELLM_MODEL", "OPENAI_MODEL", "MODEL", "MODEL_NAME"))
    llm_max_concurrency: int = 4
    llm_timeout_seconds: float = 60.0
    llm_disabled: bool = False
    # LLM provider: "auto" (Vertex AI when a Google Cloud project is set, else the OpenAI-compatible endpoint),
    # "vertex" (Gemini on Vertex AI, Application Default Credentials) or "openai_compatible".
    llm_provider: str = "auto"
    gcp_project: str | None = Field(default=None, validation_alias=AliasChoices(
        "GCP_PROJECT", "GOOGLE_CLOUD_PROJECT"))
    gcp_location: str = Field(default="global", validation_alias=AliasChoices(
        "GCP_LOCATION", "GOOGLE_CLOUD_LOCATION", "VERTEX_LOCATION"))
    vertex_model: str = "gemini-2.5-flash"

    database_url: str = "postgresql+psycopg://irc:irc_local_dev@localhost:5433/inventory_rescue"
    var_dir: str = "var"
    data_dir: str = str(ROOT_DIR / "data")
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    # File storage: "local" (var_dir) or "gcs" (Cloud Storage bucket, Application Default Credentials).
    storage_backend: str = "local"
    gcs_bucket: str | None = None
    gcs_prefix: str = ""
    # Built frontend served by FastAPI (single Cloud Run service). Ignored when the folder does not exist.
    static_dir: str = str(ROOT_DIR / "frontend" / "dist")

    default_budget_inr: float = 50_000.0
    horizon_days: int = 30
    service_level_z: float = 1.645
    max_csv_bytes: int = 2_000_000
    max_message_chars: int = 8_000

    # Service level: a unit short also costs future business (goodwill), as a share of the unit price.
    hero_share: float = 0.2  # top share of SKUs by trailing-28-day revenue treated as "hero" (priority)
    goodwill_hero_pct: float = 0.15
    goodwill_standard_pct: float = 0.05
    # Robustness evaluation: number of Monte Carlo evaluation worlds.
    eval_worlds: int = 120
    rolling_worlds: int = 20

    @property
    def resolved_llm_provider(self) -> str:
        p = self.llm_provider.strip().lower()
        if p in ("vertex", "openai_compatible"):
            return p
        return "vertex" if self.gcp_project else "openai_compatible"

    @property
    def active_llm_model(self) -> str:
        return self.vertex_model if self.resolved_llm_provider == "vertex" else self.llm_model

    @property
    def llm_configured(self) -> bool:
        if self.llm_disabled:
            return False
        if self.resolved_llm_provider == "vertex":
            return bool(self.gcp_project)
        return bool(self.litellm_base_url and self.litellm_api_key)

    @property
    def var_path(self) -> Path:
        p = Path(self.var_dir)
        return p if p.is_absolute() else ROOT_DIR / p

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()

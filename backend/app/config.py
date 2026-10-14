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

    llm_max_concurrency: int = 4
    llm_timeout_seconds: float = 60.0
    llm_disabled: bool = False
    # LLM provider: "auto" (Gemini API when a Google AI Studio key is set, else Vertex AI when a Google Cloud
    # project is set), "gemini" (Gemini API, AI Studio key) or "vertex" (Gemini on Vertex AI, Application Default
    # Credentials).
    llm_provider: str = "auto"
    gemini_api_key: str | None = Field(default=None, validation_alias=AliasChoices(
        "GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_AI_STUDIO_API_KEY", "AI_STUDIO_API_KEY", "GEMINI_KEY"))
    gemini_model: str = Field(default="gemini-3.5-flash-lite", validation_alias=AliasChoices(
        "GEMINI_MODEL", "VERTEX_MODEL"))
    # Gemini thinking level (low / medium / high); empty = the model's default.
    gemini_thinking_level: str = "low"
    # Client-side cap on Gemini requests per minute (0 = no cap). Set it just under the project's quota, e.g. 14 on
    # the AI Studio free tier (15 RPM); paid tiers rarely need it.
    gemini_rpm: int = 0
    gcp_project: str | None = Field(default=None, validation_alias=AliasChoices(
        "GCP_PROJECT", "GOOGLE_CLOUD_PROJECT", "GCP_PROJECT_ID", "GOOGLE_CLOUD_PROJECT_ID", "PROJECT_ID"))
    gcp_location: str = Field(default="global", validation_alias=AliasChoices(
        "GCP_LOCATION", "GOOGLE_CLOUD_LOCATION", "VERTEX_LOCATION"))

    # Default: a local SQLite file under var/ (no database server needed). Google Cloud: Cloud SQL for PostgreSQL.
    database_url: str = f"sqlite:///{(ROOT_DIR / 'var' / 'inventory_rescue.db').as_posix()}"
    var_dir: str = "var"
    data_dir: str = str(ROOT_DIR / "data")
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    # File storage: "local" (var_dir) or "gcs" (Cloud Storage bucket, Application Default Credentials).
    storage_backend: str = "local"
    gcs_bucket: str | None = None
    gcs_prefix: str = ""
    # Built frontend served by FastAPI (single Cloud Run service). Ignored when the folder does not exist.
    static_dir: str = str(ROOT_DIR / "frontend" / "dist")

    default_budget_inr: float = 1_000_000.0
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
    # Plan options (commit now / staggered / reserve / lean), each run with weekly re-planning.
    option_worlds: int = 10  # calm worlds; the same number of shock worlds is added
    working_capital_rate: float = 0.18  # annual cost of cash tied up in stock (used by the staggered option)
    reserve_share: float = 0.3  # share of the budget the "keep a reserve" option holds back on day 0
    lean_savings_share: float = 0.9  # "lean" = smallest budget keeping this share of the full-budget savings

    @property
    def resolved_llm_provider(self) -> str:
        p = self.llm_provider.strip().lower()
        if p in ("gemini", "vertex"):
            return p
        if self.gemini_api_key or not self.gcp_project:
            return "gemini"
        return "vertex"

    @property
    def vertex_model(self) -> str:
        return self.gemini_model

    @property
    def active_llm_model(self) -> str:
        return self.gemini_model

    @property
    def llm_configured(self) -> bool:
        if self.llm_disabled:
            return False
        if self.resolved_llm_provider == "gemini":
            return bool(self.gemini_api_key)
        return bool(self.gcp_project)

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

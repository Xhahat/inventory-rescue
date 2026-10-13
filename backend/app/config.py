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

    database_url: str = "postgresql+psycopg://irc:irc_local_dev@localhost:5433/inventory_rescue"
    var_dir: str = "var"
    data_dir: str = str(ROOT_DIR / "data")
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    default_budget_inr: float = 50_000.0
    horizon_days: int = 30
    service_level_z: float = 1.645
    max_csv_bytes: int = 2_000_000
    max_message_chars: int = 8_000

    @property
    def llm_configured(self) -> bool:
        return bool(self.litellm_base_url and self.litellm_api_key and not self.llm_disabled)

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

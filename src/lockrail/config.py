"""Application settings loaded from environment variables."""
from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="",
        extra="ignore",
    )

    # Environment
    lockrail_env: Literal["dev", "test", "prod"] = "dev"
    lockrail_log_level: str = "INFO"

    # Storage
    database_url: str
    redis_url: str

    # LLM providers
    anthropic_api_key: str | None = None
    openai_api_key: str | None = None

    # Observability
    langfuse_public_key: str | None = None
    langfuse_secret_key: str | None = None
    langfuse_host: str = "https://cloud.langfuse.com"
    otel_exporter_otlp_endpoint: str | None = None
    otel_service_name: str = "lockrail"

    # Lockrail runtime
    lockrail_idempotency_ttl_seconds: int = Field(default=86400, ge=1)
    lockrail_approval_timeout_seconds: int = Field(default=3600, ge=1)
    lockrail_dry_run_enabled: bool = True


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings — import this instead of constructing Settings() directly."""
    return Settings()  # type: ignore[call-arg]

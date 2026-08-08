"""Worker service configuration using Pydantic Settings v2.

All settings are loaded from environment variables (or .env file in development).
The ``@lru_cache`` ensures a single Settings instance is created per process.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, computed_field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class WorkerSettings(BaseSettings):
    """Worker service settings — loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        validate_default=True,
    )

    # ─── Application ──────────────────────────────────────────────────────────
    app_env: str = Field(default="development")
    log_level: str = Field(default="INFO")
    debug: bool = Field(default=False)

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        """Ensure log level is a valid Python logging level."""
        valid = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if v.upper() not in valid:
            msg = f"Invalid log level: {v!r}. Must be one of {valid}"
            raise ValueError(msg)
        return v.upper()

    # ─── Database ─────────────────────────────────────────────────────────────
    postgres_host: str = Field(default="localhost")
    postgres_port: int = Field(default=5432, ge=1, le=65535)
    postgres_user: str = Field(default="prreviewer")
    postgres_password: str = Field(default="prreviewer_pass")
    postgres_db: str = Field(default="prreviewer")

    @computed_field  # type: ignore[misc]
    @property
    def database_url(self) -> str:
        """Async PostgreSQL DSN — constructed from individual fields."""
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    # ─── Redis / Celery ───────────────────────────────────────────────────────
    redis_url: str = Field(default="redis://localhost:6379/0")
    redis_stream_name: str = Field(default="pr:events")
    redis_stream_consumer_group: str = Field(default="review-workers")
    celery_broker_url: str = Field(default="redis://localhost:6379/1")
    celery_result_backend: str = Field(default="redis://localhost:6379/2")

    # ─── LLM (OpenRouter) ─────────────────────────────────────────────────────
    openrouter_api_key: str = Field(
        ...,
        description="OpenRouter API key — get one at https://openrouter.ai",
    )
    openrouter_base_url: str = Field(
        default="https://openrouter.ai/api/v1",
    )
    primary_model: str = Field(
        default="qwen/qwen3-coder:free",
        description="Primary LLM model for code review agents.",
    )
    fallback_model: str = Field(
        default="deepseek/deepseek-v3-base:free",
        description="Fallback LLM if primary fails or times out.",
    )
    llm_max_tokens: int = Field(default=8192, ge=256, le=32768)
    llm_temperature: float = Field(default=0.1, ge=0.0, le=2.0)
    llm_request_timeout: int = Field(
        default=120, description="LLM API request timeout in seconds."
    )

    # ─── GitHub ───────────────────────────────────────────────────────────────
    github_app_id: str | None = Field(default=None)
    github_pat: str | None = Field(
        default=None,
        description="GitHub Personal Access Token (used if App auth not configured).",
    )
    internal_api_key: str = Field(
        ...,
        min_length=16,
        description="Internal API key for service-to-service calls.",
    )

    # ─── Qdrant ───────────────────────────────────────────────────────────────
    qdrant_host: str = Field(default="localhost")
    qdrant_port: int = Field(default=6333)
    qdrant_collection_name: str = Field(default="repo_conventions")
    qdrant_embedding_dim: int = Field(default=1536)

    # ─── Langfuse (LLM Observability) ─────────────────────────────────────────
    langfuse_secret_key: str | None = Field(default=None)
    langfuse_public_key: str | None = Field(default=None)
    langfuse_host: str = Field(default="http://langfuse-server:3000")

    @property
    def is_production(self) -> bool:
        """True when running in the production environment."""
        return self.app_env.lower() == "production"

    @property
    def langfuse_enabled(self) -> bool:
        """True when Langfuse tracing is configured."""
        return bool(self.langfuse_secret_key and self.langfuse_public_key)


@lru_cache(maxsize=1)
def get_settings() -> WorkerSettings:
    """Return the cached singleton WorkerSettings instance."""
    return WorkerSettings()

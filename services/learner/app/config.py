"""Learner service configuration using Pydantic Settings v2."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class LearnerSettings(BaseSettings):
    """Learner service settings — loaded from environment variables."""

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
        valid = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if v.upper() not in valid:
            msg = f"Invalid log level: {v!r}. Must be one of {valid}"
            raise ValueError(msg)
        return v.upper()

    # ─── Redis / Celery ───────────────────────────────────────────────────────
    redis_url: str = Field(default="redis://localhost:6379/0")
    celery_broker_url: str = Field(default="redis://localhost:6379/1")
    celery_result_backend: str = Field(default="redis://localhost:6379/2")

    # ─── LLM (OpenRouter) ─────────────────────────────────────────────────────
    openrouter_api_key: str = Field(...)
    openrouter_base_url: str = Field(default="https://openrouter.ai/api/v1")
    primary_model: str = Field(default="qwen/qwen3-coder:free")
    llm_max_tokens: int = Field(default=4096)
    llm_temperature: float = Field(default=0.1)
    llm_request_timeout: int = Field(default=90)

    # ─── GitHub ───────────────────────────────────────────────────────────────
    github_pat: str | None = Field(default=None)
    internal_api_key: str = Field(..., min_length=16)

    # ─── Qdrant ───────────────────────────────────────────────────────────────
    qdrant_host: str = Field(default="localhost")
    qdrant_port: int = Field(default=6333)
    qdrant_collection_name: str = Field(default="repo_conventions")
    qdrant_embedding_dim: int = Field(default=1536)

    # ─── Learning Behavior ────────────────────────────────────────────────────
    min_diff_lines_to_learn: int = Field(
        default=10,
        description="Minimum diff size to extract conventions from.",
    )
    max_conventions_per_pr: int = Field(
        default=5,
        description="Maximum number of conventions to extract per merged PR.",
    )

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() == "production"


@lru_cache(maxsize=1)
def get_settings() -> LearnerSettings:
    """Return the cached singleton LearnerSettings instance."""
    return LearnerSettings()

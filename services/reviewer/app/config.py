"""Reviewer service configuration using Pydantic Settings v2."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, computed_field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ReviewerSettings(BaseSettings):
    """Reviewer service settings — loaded from environment variables."""

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

    # ─── Database ─────────────────────────────────────────────────────────────
    postgres_host: str = Field(default="localhost")
    postgres_port: int = Field(default=5432, ge=1, le=65535)
    postgres_user: str = Field(default="prreviewer")
    postgres_password: str = Field(default="prreviewer_pass")
    postgres_db: str = Field(default="prreviewer")

    @computed_field  # type: ignore[misc]
    @property
    def database_url(self) -> str:
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    # ─── Redis / Celery ───────────────────────────────────────────────────────
    redis_url: str = Field(default="redis://localhost:6379/0")
    celery_broker_url: str = Field(default="redis://localhost:6379/1")
    celery_result_backend: str = Field(default="redis://localhost:6379/2")

    # ─── GitHub ───────────────────────────────────────────────────────────────
    github_app_id: str | None = Field(default=None)
    github_pat: str | None = Field(
        default=None,
        description="GitHub Personal Access Token.",
    )
    internal_api_key: str = Field(..., min_length=16)

    # ─── Review Behavior ──────────────────────────────────────────────────────
    max_inline_comments: int = Field(
        default=20,
        description="Maximum number of inline review comments to post per PR.",
    )
    min_severity_to_post: str = Field(
        default="low",
        description="Minimum severity level to post as a comment (low|medium|high|critical).",
    )
    post_review_summary: bool = Field(
        default=True,
        description="Whether to post an overall review summary comment.",
    )

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() == "production"


@lru_cache(maxsize=1)
def get_settings() -> ReviewerSettings:
    """Return the cached singleton ReviewerSettings instance."""
    return ReviewerSettings()

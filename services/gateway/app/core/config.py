"""Gateway service configuration using Pydantic Settings v2.

All settings are loaded from environment variables (or .env file in development).
The ``@lru_cache`` ensures a single Settings instance is created per process —
this is the canonical pattern for Pydantic Settings in FastAPI applications.

Settings are grouped logically to make the configuration surface clear:
- Application settings (env, debug, etc.)
- GitHub integration settings
- Database settings (with computed URL property)
- Redis settings
- Security settings
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, computed_field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings — loaded from environment variables.

    All fields have sensible defaults for development.
    Fields marked with ``...`` (no default) MUST be set in the environment.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",          # Ignore unknown env vars
        validate_default=True,
    )

    # ─── Application ──────────────────────────────────────────────────────────
    app_env: str = Field(
        default="development",
        description="Runtime environment: development | testing | production",
    )
    app_name: str = Field(default="pr-review-gateway")
    app_version: str = Field(default="0.1.0")
    debug: bool = Field(
        default=False,
        description="Enable debug mode (extra logging, no request validation caching)",
    )
    log_level: str = Field(default="INFO")

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        """Ensure log level is a valid Python logging level."""
        valid = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if v.upper() not in valid:
            msg = f"Invalid log level: {v!r}. Must be one of {valid}"
            raise ValueError(msg)
        return v.upper()

    # ─── GitHub ───────────────────────────────────────────────────────────────
    github_webhook_secret: str = Field(
        ...,
        description="HMAC secret configured in GitHub Webhook settings. Must match exactly.",
    )
    github_app_id: str | None = Field(
        default=None,
        description="GitHub App ID (for App-based auth). Optional if using PAT.",
    )
    github_pat: str | None = Field(
        default=None,
        description="GitHub Personal Access Token (fallback for development).",
    )

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

    @computed_field  # type: ignore[misc]
    @property
    def database_url_sync(self) -> str:
        """Sync PostgreSQL DSN — used by Alembic (which doesn't support asyncpg)."""
        return (
            f"postgresql+psycopg2://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    # ─── Redis ────────────────────────────────────────────────────────────────
    redis_url: str = Field(
        default="redis://localhost:6379/0",
        description="Redis connection URL for the event stream.",
    )
    redis_stream_name: str = Field(
        default="pr:events",
        description="Redis Stream key for PR review events.",
    )
    redis_max_stream_length: int = Field(
        default=10_000,
        description="Approximate maximum length of the Redis stream (MAXLEN ~).",
    )

    # ─── Security ─────────────────────────────────────────────────────────────
    internal_api_key: str = Field(
        ...,
        min_length=16,
        description="API key for internal service-to-service calls.",
    )

    # ─── Rate Limiting ────────────────────────────────────────────────────────
    max_webhook_events_per_minute: int = Field(
        default=100,
        description="Maximum accepted webhook events per source IP per minute.",
    )

    # ─── Server ───────────────────────────────────────────────────────────────
    host: str = Field(default="0.0.0.0")  # noqa: S104
    port: int = Field(default=8000)

    @property
    def is_production(self) -> bool:
        """True when running in the production environment."""
        return self.app_env.lower() == "production"

    @property
    def is_testing(self) -> bool:
        """True when running tests."""
        return self.app_env.lower() == "testing"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached singleton Settings instance.

    The ``@lru_cache`` means this is called once per process. In tests,
    use ``get_settings.cache_clear()`` + monkeypatching to override values.
    """
    return Settings()

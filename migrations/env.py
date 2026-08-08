"""Alembic migration environment configuration.

This file is loaded by Alembic when running ``alembic upgrade head`` or
any other migration command. It:

1. Reads the database URL from environment variables (not alembic.ini)
2. Imports all ORM models so Alembic can generate autogenerate diffs
3. Sets up the migration context (online mode for normal use)

Environment variables required:
- POSTGRES_HOST, POSTGRES_PORT, POSTGRES_USER, POSTGRES_PASSWORD, POSTGRES_DB
- OR DATABASE_URL_SYNC (takes precedence)

Note: Alembic uses synchronous SQLAlchemy (psycopg2), NOT asyncpg.
The DATABASE_URL_SYNC uses postgresql+psycopg2:// driver.
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# Alembic Config object — provides access to values in alembic.ini
config = context.config

# Set up Python logging from the alembic.ini [loggers] section
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# ─── Import all models so Alembic can detect changes ─────────────────────────
# IMPORTANT: Every new ORM model must be imported here, otherwise
# Alembic's autogenerate will not detect it.
from shared.infrastructure.database import Base  # noqa: E402
from shared.infrastructure.orm_models import (  # noqa: E402, F401
    PREventRecord,
    ReviewJobRecord,
)

target_metadata = Base.metadata

# ─── Database URL from environment ────────────────────────────────────────────


def get_database_url() -> str:
    """Construct the synchronous PostgreSQL URL from environment variables.

    Uses psycopg2 driver (synchronous) because Alembic does not support
    async connections. asyncpg is used only in the application services.
    """
    # Allow explicit override via DATABASE_URL_SYNC env var
    if url := os.environ.get("DATABASE_URL_SYNC"):
        return url

    host = os.environ.get("POSTGRES_HOST", "localhost")
    port = os.environ.get("POSTGRES_PORT", "5432")
    user = os.environ.get("POSTGRES_USER", "prreviewer")
    password = os.environ.get("POSTGRES_PASSWORD", "prreviewer_pass")
    db = os.environ.get("POSTGRES_DB", "prreviewer")

    return f"postgresql+psycopg2://{user}:{password}@{host}:{port}/{db}"


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    In offline mode, Alembic emits SQL to stdout (or a file) without
    connecting to the database. Useful for reviewing migration SQL before
    applying it.

    Usage: alembic upgrade head --sql
    """
    url = get_database_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        # Include schemas for multi-schema support
        include_schemas=False,
        # Naming conventions for auto-generated constraints
        render_as_batch=False,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode (normal usage).

    Connects to the database and applies migrations directly.
    """
    # Override the sqlalchemy.url from alembic.ini with the env-var URL
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = get_database_url()

    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,  # No connection pooling in migration scripts
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # Compare server defaults (e.g. func.now()) in autogenerate
            compare_server_default=True,
            # Compare column types strictly
            compare_type=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

"""Async SQLAlchemy 2.0 database setup.

Design decisions:
- Uses ``create_async_engine`` with ``asyncpg`` driver for full async I/O.
- Connection pool is tuned for production: pool_size=10, max_overflow=20.
- ``expire_on_commit=False`` prevents lazy-load errors after commit in async context.
- All sessions are created via ``AsyncSessionLocal`` — never use sync sessions.
- The ``get_db_session`` async context manager is used in both FastAPI dependency
  injection and standalone scripts (Celery tasks).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    """Base class for all SQLAlchemy ORM models.

    Using the new-style declarative base from SQLAlchemy 2.0.
    All ORM models must inherit from this class.
    """


# Module-level engine and session factory — initialized once at startup.
_engine: AsyncEngine | None = None
_async_session_factory: async_sessionmaker[AsyncSession] | None = None


def init_database(database_url: str, *, echo: bool = False) -> None:
    """Initialize the database engine and session factory.

    Must be called once at application startup (in lifespan context manager).

    Args:
        database_url: PostgreSQL async DSN (postgresql+asyncpg://...).
        echo: If True, log all SQL statements (debug only).
    """
    global _engine, _async_session_factory  # noqa: PLW0603

    if _engine is not None:
        logger.warning("Database already initialized — skipping re-initialization")
        return

    logger.info("Initializing database connection pool", extra={"url": _redact_url(database_url)})

    _engine = create_async_engine(
        database_url,
        echo=echo,
        pool_size=10,
        max_overflow=20,
        pool_pre_ping=True,          # Reconnect on stale connections
        pool_recycle=3600,           # Recycle connections after 1 hour
        connect_args={
            "server_settings": {
                "application_name": "pr_reviewer",
                "jit": "off",        # Disable JIT for short-lived queries
            }
        },
    )

    _async_session_factory = async_sessionmaker(
        bind=_engine,
        class_=AsyncSession,
        expire_on_commit=False,      # Critical for async — prevents implicit I/O
        autoflush=True,
        autocommit=False,
    )

    logger.info("Database connection pool initialized")


async def close_database() -> None:
    """Gracefully close the database engine.

    Call this in the application shutdown lifespan handler.
    """
    global _engine, _async_session_factory  # noqa: PLW0603

    if _engine is not None:
        logger.info("Closing database connection pool")
        await _engine.dispose()
        _engine = None
        _async_session_factory = None
        logger.info("Database connection pool closed")


def get_engine() -> AsyncEngine:
    """Return the initialized engine, raising if not initialized."""
    if _engine is None:
        msg = "Database not initialized. Call init_database() first."
        raise RuntimeError(msg)
    return _engine


@asynccontextmanager
async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    """Async context manager that provides a database session.

    Handles commit on success, rollback on exception, and always closes
    the session. Use this in Celery tasks and background jobs.

    Example::

        async with get_db_session() as session:
            result = await session.execute(select(PREventRecord))

    For FastAPI routes, use the ``get_db`` dependency instead, which is
    a thin wrapper around this function.
    """
    if _async_session_factory is None:
        msg = "Database not initialized. Call init_database() first."
        raise RuntimeError(msg)

    async with _async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that yields an async database session.

    Example::

        @router.get("/events")
        async def list_events(db: AsyncSession = Depends(get_db)):
            ...
    """
    async with get_db_session() as session:
        yield session


def _redact_url(url: str) -> str:
    """Redact password from database URL for safe logging."""
    try:
        from urllib.parse import urlparse, urlunparse
        parsed = urlparse(url)
        if parsed.password:
            redacted = parsed._replace(
                netloc=f"{parsed.username}:***@{parsed.hostname}:{parsed.port}"
            )
            return urlunparse(redacted)
    except Exception:  # noqa: BLE001
        pass
    return url

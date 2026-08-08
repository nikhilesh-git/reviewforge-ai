"""FastAPI dependency injection providers for the gateway service.

All shared resources (database sessions, Redis client, settings) are
provided through FastAPI's dependency injection system. This enables:
- Clean separation between infrastructure setup and request handling
- Easy mocking in tests (override dependencies with ``app.dependency_overrides``)
- Proper resource lifecycle management (connections released after each request)

Usage in route handlers::

    @router.post("/webhooks/github")
    async def handle_webhook(
        redis: RedisClient = Depends(get_redis),
        db: AsyncSession = Depends(get_db),
        settings: Settings = Depends(get_settings),
    ):
        ...
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Annotated

from fastapi import Depends
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from shared.infrastructure.database import get_db
from shared.infrastructure.redis_client import get_redis_client

from .config import Settings, get_settings

# ─── Type aliases for cleaner route signatures ───────────────────────────────

SettingsDep = Annotated[Settings, Depends(get_settings)]


async def get_redis() -> AsyncGenerator[Redis, None]:  # type: ignore[type-arg]
    """Provide the shared Redis client as a FastAPI dependency.

    The Redis client uses a connection pool — this dependency does NOT
    open a new connection for each request. It simply returns the module-level
    client. The pool manages connection reuse automatically.

    Yields:
        The initialized Redis client.

    Raises:
        RuntimeError: If init_redis() was not called at startup.
    """
    yield get_redis_client()


async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    """Provide an async database session as a FastAPI dependency.

    This is a thin wrapper around ``shared.infrastructure.database.get_db``
    that makes the dependency injectable via FastAPI's DI system.

    The session is committed on success and rolled back on exception.

    Yields:
        An active ``AsyncSession``.
    """
    async for session in get_db():
        yield session


# Annotated dependency types for use in route function signatures
RedisDep = Annotated[Redis, Depends(get_redis)]  # type: ignore[type-arg]
DBSessionDep = Annotated[AsyncSession, Depends(get_db_session)]

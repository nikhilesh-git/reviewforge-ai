"""Async Redis client with connection pooling and stream support.

Design decisions:
- Uses ``redis.asyncio`` (built into redis-py 4.2+) for true async I/O.
- Single connection pool shared across the application via module-level singleton.
- Typed helper methods for stream operations to avoid raw Redis command usage.
- Stream consumer groups for reliable event delivery with acknowledgment.
- ``decode_responses=False`` is intentional — we handle encoding explicitly
  to avoid hidden encoding surprises with binary data.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

import redis.asyncio as aioredis
from redis.asyncio import Redis
from redis.asyncio.connection import ConnectionPool

logger = logging.getLogger(__name__)

# Module-level singleton — initialized once at application startup
_redis_pool: ConnectionPool | None = None
_redis_client: Redis | None = None  # type: ignore[type-arg]


def init_redis(redis_url: str, *, max_connections: int = 50) -> None:
    """Initialize the Redis connection pool.

    Args:
        redis_url: Redis connection URL (e.g. redis://localhost:6379/0)
        max_connections: Maximum concurrent connections in the pool.
    """
    global _redis_pool, _redis_client  # noqa: PLW0603

    if _redis_pool is not None:
        logger.warning("Redis already initialized — skipping re-initialization")
        return

    logger.info("Initializing Redis connection pool", extra={"url": _redact_url(redis_url)})

    _redis_pool = ConnectionPool.from_url(
        redis_url,
        max_connections=max_connections,
        decode_responses=True,       # Return str instead of bytes
        socket_connect_timeout=5,
        socket_timeout=10,
        retry_on_timeout=True,
        health_check_interval=30,
    )
    _redis_client = Redis(connection_pool=_redis_pool)

    logger.info("Redis connection pool initialized")


async def close_redis() -> None:
    """Close all Redis connections gracefully."""
    global _redis_pool, _redis_client  # noqa: PLW0603

    if _redis_client is not None:
        logger.info("Closing Redis connections")
        await _redis_client.aclose()
        if _redis_pool is not None:
            await _redis_pool.aclose()
        _redis_client = None
        _redis_pool = None
        logger.info("Redis connections closed")


def get_redis_client() -> Redis:  # type: ignore[type-arg]
    """Return the initialized Redis client, raising if not initialized."""
    if _redis_client is None:
        msg = "Redis not initialized. Call init_redis() first."
        raise RuntimeError(msg)
    return _redis_client


async def ping_redis() -> bool:
    """Check Redis connectivity. Returns True if reachable."""
    try:
        client = get_redis_client()
        result = await client.ping()
        return bool(result)
    except Exception as exc:
        logger.error("Redis ping failed", extra={"error": str(exc)})
        return False


# ─── Stream Operations ────────────────────────────────────────────────────────


async def stream_publish(
    client: Redis,  # type: ignore[type-arg]
    stream_name: str,
    fields: dict[str, str],
    *,
    max_length: int = 10_000,
) -> str:
    """Publish a message to a Redis Stream.

    Args:
        client: Redis client instance.
        stream_name: Redis stream key (e.g. "pr:events").
        fields: Dict of string key-value pairs to store in the stream entry.
        max_length: Approximate maximum stream length (uses MAXLEN ~ for efficiency).

    Returns:
        The Redis stream entry ID (e.g. "1704067200000-0").
    """
    entry_id = await client.xadd(
        stream_name,
        fields,
        maxlen=max_length,
        approximate=True,  # MAXLEN ~ is O(1) amortized vs O(N)
    )
    logger.debug(
        "Published to stream",
        extra={"stream": stream_name, "entry_id": entry_id, "fields": list(fields.keys())},
    )
    return entry_id


async def ensure_consumer_group(
    client: Redis,  # type: ignore[type-arg]
    stream_name: str,
    group_name: str,
) -> None:
    """Create a consumer group if it doesn't already exist.

    Uses XGROUP CREATE with MKSTREAM to create the stream if it doesn't exist.
    The '$' starting ID means new consumers only see events published after
    the group was created (not historical events).

    Args:
        client: Redis client instance.
        stream_name: Redis stream key.
        group_name: Consumer group name.
    """
    try:
        await client.xgroup_create(
            stream_name,
            group_name,
            id="$",      # Start from newest entry
            mkstream=True,
        )
        logger.info(
            "Created consumer group",
            extra={"stream": stream_name, "group": group_name},
        )
    except aioredis.ResponseError as exc:
        if "BUSYGROUP" in str(exc):
            logger.debug(
                "Consumer group already exists",
                extra={"stream": stream_name, "group": group_name},
            )
        else:
            raise


async def stream_read_group(
    client: Redis,  # type: ignore[type-arg]
    stream_name: str,
    group_name: str,
    consumer_name: str,
    *,
    count: int = 10,
    block_ms: int = 5000,
) -> list[tuple[str, dict[str, str]]]:
    """Read messages from a stream as a member of a consumer group.

    Args:
        client: Redis client.
        stream_name: Redis stream key.
        group_name: Consumer group name.
        consumer_name: This consumer's unique name (e.g. "worker-1").
        count: Maximum number of messages to fetch.
        block_ms: Milliseconds to block waiting for messages (0=non-blocking).

    Returns:
        List of (entry_id, fields) tuples.
    """
    result = await client.xreadgroup(
        groupname=group_name,
        consumername=consumer_name,
        streams={stream_name: ">"},  # ">" = undelivered messages only
        count=count,
        block=block_ms,
    )

    entries: list[tuple[str, dict[str, str]]] = []
    if result:
        for _stream, messages in result:
            for entry_id, fields in messages:
                entries.append((entry_id, fields))

    return entries


async def stream_acknowledge(
    client: Redis,  # type: ignore[type-arg]
    stream_name: str,
    group_name: str,
    entry_id: str,
) -> None:
    """Acknowledge successful processing of a stream message.

    After ACK, the message is removed from the Pending Entry List (PEL).
    Messages NOT acknowledged will be redelivered on next XREADGROUP call.
    """
    await client.xack(stream_name, group_name, entry_id)
    logger.debug(
        "Acknowledged stream entry",
        extra={"stream": stream_name, "entry_id": entry_id},
    )


def _redact_url(url: str) -> str:
    """Redact password from Redis URL for safe logging."""
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

"""Pytest fixtures for the gateway service tests.

Architecture:
- Tests use FastAPI's ``AsyncClient`` (from ``httpx``) for full ASGI testing
  without starting a real server.
- Database is mocked via ``AsyncMock`` — unit tests never touch a real DB.
- Redis is mocked via ``AsyncMock`` — no real Redis needed for unit tests.
- Integration tests (marked with ``@pytest.mark.integration``) use real
  services via environment variables.

Fixture hierarchy:
  app → override_dependencies → client
  settings → provides test configuration
  hmac_signer → generates valid HMAC signatures for test payloads
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core.config import Settings, get_settings
from app.core.dependencies import get_db_session, get_redis


# ─── Settings fixture ─────────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def test_settings() -> Settings:
    """Provide test-specific settings.

    Uses environment variables if available (for CI), otherwise falls back
    to sensible test defaults. The ``get_settings.cache_clear()`` call
    ensures these settings are used instead of the cached production settings.
    """
    get_settings.cache_clear()
    settings = Settings(
        app_env="testing",
        app_name="pr-review-gateway-test",
        app_version="0.0.1-test",
        debug=True,
        log_level="DEBUG",
        github_webhook_secret="test-webhook-secret-for-hmac",
        postgres_host="localhost",
        postgres_port=5432,
        postgres_user="test_user",
        postgres_password="test_pass",
        postgres_db="test_db",
        redis_url="redis://localhost:6379/0",
        redis_stream_name="pr:events:test",
        redis_max_stream_length=100,
        internal_api_key="test-internal-api-key-minimum-length",
    )
    yield settings
    get_settings.cache_clear()


# ─── Mock dependencies ────────────────────────────────────────────────────────


@pytest.fixture
def mock_redis() -> AsyncMock:
    """Provide a mock Redis client that doesn't require a real Redis instance."""
    mock = AsyncMock()
    mock.xadd = AsyncMock(return_value="1704067200000-0")
    mock.ping = AsyncMock(return_value=True)
    mock.xreadgroup = AsyncMock(return_value=None)
    mock.xack = AsyncMock(return_value=1)
    return mock


@pytest.fixture
def mock_db_session() -> AsyncMock:
    """Provide a mock database session that doesn't require a real PostgreSQL."""
    mock = AsyncMock()
    mock.execute = AsyncMock()
    mock.commit = AsyncMock()
    mock.rollback = AsyncMock()
    mock.close = AsyncMock()
    return mock


@pytest.fixture
def mock_event_repo(mock_db_session: AsyncMock) -> MagicMock:
    """Provide a mock EventRepository."""
    from app.repositories.event_repository import EventRepository
    from unittest.mock import create_autospec

    repo = create_autospec(EventRepository, instance=True)
    repo.save_received = AsyncMock(return_value=MagicMock(id="test-uuid"))
    repo.mark_queued = AsyncMock()
    repo.mark_ignored = AsyncMock()
    repo.mark_failed = AsyncMock()
    repo.exists_by_unique_key = AsyncMock(return_value=False)
    return repo


# ─── App fixture ──────────────────────────────────────────────────────────────


@pytest.fixture
def app(test_settings: Settings, mock_redis: AsyncMock, mock_db_session: AsyncMock) -> FastAPI:
    """Create a test FastAPI application with mocked dependencies.

    Overrides Redis and DB dependencies so tests never touch real infrastructure.
    """
    from app.main import create_app

    test_app = create_app()

    # Override settings
    test_app.dependency_overrides[get_settings] = lambda: test_settings

    # Override Redis
    async def get_mock_redis() -> AsyncGenerator:
        yield mock_redis

    test_app.dependency_overrides[get_redis] = get_mock_redis

    # Override DB session
    async def get_mock_db() -> AsyncGenerator:
        yield mock_db_session

    test_app.dependency_overrides[get_db_session] = get_mock_db

    return test_app


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncGenerator[AsyncClient, None]:
    """Provide an async HTTP test client.

    Uses ASGI transport — no real HTTP server is started.
    All requests go through the FastAPI ASGI interface directly.
    """
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as ac:
        yield ac


# ─── HMAC Helper ──────────────────────────────────────────────────────────────


class HMACSigner:
    """Helper for generating valid GitHub webhook signatures in tests."""

    def __init__(self, secret: str) -> None:
        self._secret = secret.encode("utf-8")

    def sign(self, payload: bytes) -> str:
        """Compute the HMAC-SHA256 signature for a payload.

        Returns:
            Signature header value: ``sha256=<hex_digest>``
        """
        digest = hmac.new(
            key=self._secret,
            msg=payload,
            digestmod=hashlib.sha256,
        ).hexdigest()
        return f"sha256={digest}"

    def sign_json(self, payload: dict[str, Any]) -> tuple[bytes, str]:
        """Serialize payload to JSON bytes and compute signature.

        Returns:
            Tuple of (raw_bytes, signature_header_value)
        """
        raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        return raw, self.sign(raw)


@pytest.fixture
def hmac_signer(test_settings: Settings) -> HMACSigner:
    """Provide an HMAC signer configured with the test webhook secret."""
    return HMACSigner(test_settings.github_webhook_secret)


# ─── Payload Factories ────────────────────────────────────────────────────────


@pytest.fixture
def pr_opened_payload() -> dict[str, Any]:
    """Realistic GitHub pull_request opened webhook payload."""
    return {
        "action": "opened",
        "number": 42,
        "pull_request": {
            "number": 42,
            "title": "feat: add user authentication",
            "body": "This PR adds JWT-based user authentication",
            "html_url": "https://github.com/test-org/test-repo/pull/42",
            "state": "open",
            "merged": False,
            "head": {
                "sha": "a" * 40,
                "ref": "refs/heads/feature/auth",
                "label": "test-org:feature/auth",
            },
            "base": {
                "sha": "b" * 40,
                "ref": "refs/heads/main",
                "label": "test-org:main",
            },
            "user": {
                "login": "developer-alice",
                "avatar_url": "https://github.com/avatars/developer-alice",
                "type": "User",
            },
        },
        "repository": {
            "id": 123456,
            "full_name": "test-org/test-repo",
            "name": "test-repo",
            "private": False,
            "html_url": "https://github.com/test-org/test-repo",
        },
        "sender": {
            "login": "developer-alice",
            "type": "User",
        },
        "installation": {
            "id": 98765,
        },
    }


@pytest.fixture
def pr_synchronize_payload(pr_opened_payload: dict[str, Any]) -> dict[str, Any]:
    """GitHub pull_request synchronize (new commit pushed) payload."""
    payload = dict(pr_opened_payload)
    payload["action"] = "synchronize"
    payload["pull_request"]["head"]["sha"] = "c" * 40
    return payload


@pytest.fixture
def pr_closed_not_merged_payload(pr_opened_payload: dict[str, Any]) -> dict[str, Any]:
    """GitHub pull_request closed (without merge) payload."""
    payload = dict(pr_opened_payload)
    payload["action"] = "closed"
    payload["pull_request"]["state"] = "closed"
    payload["pull_request"]["merged"] = False
    return payload


@pytest.fixture
def pr_merged_payload(pr_opened_payload: dict[str, Any]) -> dict[str, Any]:
    """GitHub pull_request closed (merged) payload."""
    payload = dict(pr_opened_payload)
    payload["action"] = "closed"
    payload["pull_request"]["state"] = "closed"
    payload["pull_request"]["merged"] = True
    return payload

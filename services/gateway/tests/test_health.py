"""Tests for the health check endpoints.

Verifies:
- GET /health returns 200 with correct schema
- GET /health/ready checks Redis and DB connectivity
- GET /metrics returns Prometheus text format
- Uptime tracking works correctly
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


class TestLivenessProbe:
    """Tests for the /health (liveness) endpoint."""

    async def test_health_returns_200(self, client: AsyncClient) -> None:
        """GET /health must always return 200."""
        response = await client.get("/health")
        assert response.status_code == 200

    async def test_health_response_schema(self, client: AsyncClient) -> None:
        """Health response must include all required fields."""
        response = await client.get("/health")
        body = response.json()

        assert body["status"] == "ok"
        assert "service" in body
        assert "version" in body
        assert "environment" in body
        assert "uptime_seconds" in body
        assert "timestamp" in body
        assert body["environment"] == "testing"

    async def test_health_uptime_is_positive(self, client: AsyncClient) -> None:
        """Uptime must be a positive number."""
        response = await client.get("/health")
        uptime = response.json()["uptime_seconds"]
        assert isinstance(uptime, float)
        assert uptime >= 0.0

    async def test_health_timestamp_is_iso_format(self, client: AsyncClient) -> None:
        """Timestamp must be in ISO 8601 format."""
        from datetime import datetime
        response = await client.get("/health")
        ts = response.json()["timestamp"]
        # Should not raise
        datetime.fromisoformat(ts)


class TestReadinessProbe:
    """Tests for the /health/ready (readiness) endpoint."""

    async def test_readiness_ok_when_all_deps_up(self, client: AsyncClient) -> None:
        """Readiness probe returns 200 when Redis and DB are reachable."""
        with (
            patch("app.api.health.ping_redis", return_value=True),
            patch("app.api.health.get_engine") as mock_engine,
        ):
            mock_conn = AsyncMock()
            mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
            mock_conn.__aexit__ = AsyncMock(return_value=None)
            mock_conn.execute = AsyncMock()
            mock_engine.return_value.connect = MagicMock(return_value=mock_conn)

            response = await client.get("/health/ready")

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert len(body["checks"]) == 2  # Redis + DB
        assert all(c["status"] == "ok" for c in body["checks"])

    async def test_readiness_503_when_redis_down(self, client: AsyncClient) -> None:
        """Readiness probe returns 503 when Redis is unreachable."""
        with (
            patch("app.api.health.ping_redis", side_effect=Exception("Connection refused")),
            patch("app.api.health.get_engine") as mock_engine,
        ):
            mock_conn = AsyncMock()
            mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
            mock_conn.__aexit__ = AsyncMock(return_value=None)
            mock_conn.execute = AsyncMock()
            mock_engine.return_value.connect = MagicMock(return_value=mock_conn)

            response = await client.get("/health/ready")

        assert response.status_code == 503
        body = response.json()
        assert body["status"] == "down"
        redis_check = next(c for c in body["checks"] if c["name"] == "redis")
        assert redis_check["status"] == "down"

    async def test_readiness_includes_latency_for_each_check(
        self, client: AsyncClient
    ) -> None:
        """Each check in the readiness response must include latency_ms."""
        with (
            patch("app.api.health.ping_redis", return_value=True),
            patch("app.api.health.get_engine") as mock_engine,
        ):
            mock_conn = AsyncMock()
            mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
            mock_conn.__aexit__ = AsyncMock(return_value=None)
            mock_conn.execute = AsyncMock()
            mock_engine.return_value.connect = MagicMock(return_value=mock_conn)

            response = await client.get("/health/ready")

        body = response.json()
        for check in body["checks"]:
            assert "latency_ms" in check
            assert isinstance(check["latency_ms"], float)
            assert check["latency_ms"] >= 0.0


class TestPrometheusMetrics:
    """Tests for the /metrics Prometheus endpoint."""

    async def test_metrics_endpoint_returns_200(self, client: AsyncClient) -> None:
        """GET /metrics must return 200."""
        response = await client.get("/metrics")
        assert response.status_code == 200

    async def test_metrics_content_type_is_prometheus(self, client: AsyncClient) -> None:
        """Metrics must use the Prometheus text exposition content type."""
        response = await client.get("/metrics")
        assert "text/plain" in response.headers["content-type"]

    async def test_metrics_contains_gateway_counters(self, client: AsyncClient) -> None:
        """Metrics response must contain at least the gateway counters."""
        response = await client.get("/metrics")
        text = response.text
        assert "gateway_webhook_requests_total" in text
        assert "gateway_events_published_total" in text
        assert "gateway_hmac_failures_total" in text

    async def test_metrics_not_in_openapi_docs(self, client: AsyncClient) -> None:
        """The /metrics endpoint must NOT appear in the OpenAPI spec."""
        response = await client.get("/openapi.json")
        if response.status_code == 200:
            paths = response.json().get("paths", {})
            assert "/metrics" not in paths

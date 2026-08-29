"""Health check and readiness probe endpoints.

These endpoints serve two purposes:
1. Docker health checks (``HEALTHCHECK CMD curl -f http://localhost:8000/health``)
2. Kubernetes-style readiness/liveness probes (future use)

Health check design:
- ``/health`` — Liveness probe: is the process up? Always returns 200 if the
  app is running. Does NOT check external dependencies (Redis, DB) because
  a temporarily unreachable dependency should not kill the container.
- ``/health/ready`` — Readiness probe: are all dependencies reachable? Returns
  200 only when the gateway can accept traffic. Used by load balancers.
- ``/metrics`` — Prometheus metrics endpoint (text exposition format).

Response times must be fast (< 100ms) — health checks run every 30s.
"""

from __future__ import annotations

import platform
import time
from datetime import datetime, timezone

UTC = timezone.utc
from typing import Any

import httpx
import structlog
from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from sqlalchemy import text

from shared.infrastructure.database import get_engine
from shared.infrastructure.redis_client import ping_redis

from ..core.config import Settings, get_settings
from ..core.dependencies import SettingsDep

logger = structlog.get_logger(__name__)
router = APIRouter(tags=["Health & Monitoring"])

# Record startup time for uptime calculation
_startup_time = time.time()


# ─── Response Models ──────────────────────────────────────────────────────────


class ServiceStatus(BaseModel):
    """Status of a single dependency."""

    name: str
    status: str  # "ok" | "degraded" | "down"
    latency_ms: float | None = None
    detail: str | None = None


class HealthResponse(BaseModel):
    """Health check response body."""

    status: str  # "ok" | "degraded" | "down"
    service: str
    version: str
    environment: str
    uptime_seconds: float
    timestamp: str
    checks: list[ServiceStatus] = []


# ─── Liveness Probe ───────────────────────────────────────────────────────────


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Liveness probe",
    description="Returns 200 OK if the gateway process is running.",
)
async def health_check(settings: SettingsDep) -> HealthResponse:
    """Liveness probe — always 200 if the process is alive.

    This endpoint intentionally does NOT check external dependencies.
    A failing Redis or DB connection should not restart the gateway container —
    the gateway queues events and will retry when dependencies recover.
    """
    return HealthResponse(
        status="ok",
        service=settings.app_name,
        version=settings.app_version,
        environment=settings.app_env,
        uptime_seconds=round(time.time() - _startup_time, 2),
        timestamp=datetime.now(UTC).isoformat(),
    )


# ─── Readiness Probe ──────────────────────────────────────────────────────────


@router.get(
    "/health/ready",
    response_model=HealthResponse,
    responses={
        200: {"description": "All dependencies are reachable"},
        503: {"description": "One or more dependencies are unavailable"},
    },
    summary="Readiness probe",
    description="Returns 200 only when all dependencies (Redis, DB) are reachable.",
)
async def readiness_check(response: Response, settings: SettingsDep) -> HealthResponse:
    """Readiness probe — checks all external dependencies."""
    checks: list[ServiceStatus] = []
    overall_status = "ok"

    # ── Redis check ───────────────────────────────────────────────────────────
    redis_start = time.perf_counter()
    try:
        redis_ok = await ping_redis()
        redis_latency = (time.perf_counter() - redis_start) * 1000
        checks.append(
            ServiceStatus(
                name="redis",
                status="ok" if redis_ok else "down",
                latency_ms=round(redis_latency, 2),
            )
        )
        if not redis_ok:
            overall_status = "down"
    except Exception as exc:
        redis_latency = (time.perf_counter() - redis_start) * 1000
        checks.append(
            ServiceStatus(
                name="redis",
                status="down",
                latency_ms=round(redis_latency, 2),
                detail=str(exc)[:200],
            )
        )
        overall_status = "down"

    # ── Database check (lightweight ping) ─────────────────────────────────────
    db_start = time.perf_counter()
    try:
        engine = get_engine()
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        db_latency = (time.perf_counter() - db_start) * 1000
        checks.append(
            ServiceStatus(
                name="database",
                status="ok",
                latency_ms=round(db_latency, 2),
            )
        )
    except Exception as exc:
        db_latency = (time.perf_counter() - db_start) * 1000
        checks.append(
            ServiceStatus(
                name="database",
                status="down",
                latency_ms=round(db_latency, 2),
                detail=str(exc)[:200],
            )
        )
        overall_status = "down"

    # ── Qdrant check ──────────────────────────────────────────────────────────
    qdrant_start = time.perf_counter()
    try:
        qdrant_base = settings.qdrant_url if settings.qdrant_url else f"http://{settings.qdrant_host}:{settings.qdrant_port}"
        async with httpx.AsyncClient(timeout=3.0) as client:
            # Note: Qdrant cloud uses api-key header
            headers = {"api-key": settings.qdrant_api_key} if getattr(settings, "qdrant_api_key", None) else {}
            # /readyz is the standard Qdrant health check endpoint
            resp = await client.get(f"{qdrant_base.rstrip('/')}/readyz", headers=headers)
            resp.raise_for_status()
        qdrant_latency = (time.perf_counter() - qdrant_start) * 1000
        checks.append(ServiceStatus(name="qdrant", status="ok", latency_ms=round(qdrant_latency, 2)))
    except Exception as exc:
        qdrant_latency = (time.perf_counter() - qdrant_start) * 1000
        checks.append(ServiceStatus(name="qdrant", status="down", latency_ms=round(qdrant_latency, 2), detail=str(exc)[:200]))
        overall_status = "down"

    # ── OpenRouter check ──────────────────────────────────────────────────────
    if getattr(settings, "openrouter_api_key", None):
        or_start = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                headers = {"Authorization": f"Bearer {settings.openrouter_api_key}"}
                resp = await client.get(f"{settings.openrouter_base_url.rstrip('/')}/auth/key", headers=headers)
                resp.raise_for_status()
            or_latency = (time.perf_counter() - or_start) * 1000
            checks.append(ServiceStatus(name="openrouter", status="ok", latency_ms=round(or_latency, 2)))
        except Exception as exc:
            or_latency = (time.perf_counter() - or_start) * 1000
            checks.append(ServiceStatus(name="openrouter", status="down", latency_ms=round(or_latency, 2), detail=str(exc)[:200]))
            overall_status = "down"

    if overall_status != "ok":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return HealthResponse(
        status=overall_status,
        service=settings.app_name,
        version=settings.app_version,
        environment=settings.app_env,
        uptime_seconds=round(time.time() - _startup_time, 2),
        timestamp=datetime.now(UTC).isoformat(),
        checks=checks,
    )


# ─── Prometheus Metrics ────────────────────────────────────────────────────────


@router.get(
    "/metrics",
    include_in_schema=False,  # Hide from OpenAPI docs
    summary="Prometheus metrics",
    description="Prometheus text exposition format metrics endpoint.",
)
async def prometheus_metrics() -> Response:
    """Serve Prometheus metrics in text exposition format.

    Scraped by Prometheus every 15 seconds.
    All gateway metrics are registered in ``app.core.metrics``.
    """
    return Response(
        content=generate_latest(),
        media_type=CONTENT_TYPE_LATEST,
    )

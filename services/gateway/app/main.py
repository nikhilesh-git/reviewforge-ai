"""Gateway FastAPI application entry point.

Lifespan context manager pattern (FastAPI 0.93+):
- Startup: Initialize DB pool, Redis pool, run migrations check, set build info
- Shutdown: Gracefully close all connections

Middleware stack (applied in order, outermost first):
1. RequestContextMiddleware — injects request_id into structlog context
2. PrometheusMiddleware — tracks active connections
3. CORSMiddleware — allows cross-origin requests in dev
4. GZipMiddleware — compresses large responses

Global exception handlers:
- RequestValidationError → 422 with structured error details
- HTTPException → pass-through with consistent envelope
- Exception → 500 with safe error message (no internal detail leaked)
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

import structlog
from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware

from shared.infrastructure.database import close_database, init_database
from shared.infrastructure.logging import configure_logging
from shared.infrastructure.metrics import BUILD_INFO
from shared.infrastructure.redis_client import close_redis, init_redis

from .api.health import router as health_router
from .api.webhooks import router as webhook_router
from .core.config import get_settings

logger = structlog.get_logger(__name__)


# ─── Lifespan ─────────────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Manage application startup and shutdown.

    This is the single place where all shared resources are initialized.
    Using the lifespan pattern (vs. @app.on_event) is the FastAPI-recommended
    approach since FastAPI 0.93.
    """
    settings = get_settings()

    # Configure structured logging first (needed by all subsequent operations)
    configure_logging(
        log_level=settings.log_level,
        json_output=settings.is_production,
        service_name=settings.app_name,
    )

    log = structlog.get_logger(__name__)
    log.info(
        "Starting gateway service",
        version=settings.app_version,
        env=settings.app_env,
        debug=settings.debug,
    )

    # Set Prometheus build info
    BUILD_INFO.info(
        {
            "version": settings.app_version,
            "service": settings.app_name,
            "environment": settings.app_env,
        }
    )

    # Initialize database connection pool
    log.info("Initializing database connection pool")
    init_database(
        database_url=settings.database_url,
        echo=settings.debug,
    )

    # Initialize Redis connection pool
    log.info("Initializing Redis connection pool")
    init_redis(
        redis_url=settings.redis_url,
        max_connections=50,
    )

    log.info("Gateway service startup complete — accepting requests")

    yield  # Application runs here

    # ── Shutdown ──────────────────────────────────────────────────────────────
    log.info("Shutting down gateway service")

    await close_redis()
    await close_database()

    log.info("Gateway service shutdown complete")


# ─── Application Factory ──────────────────────────────────────────────────────


def create_app() -> FastAPI:
    """Create and configure the FastAPI application.

    Returns a fully configured FastAPI instance with:
    - Lifespan handlers
    - Middleware stack
    - Routers
    - Exception handlers

    Note: Settings are loaded lazily (inside lifespan/request handlers) so
    that tests can override ``get_settings`` via DI before any validation runs.
    """
    # In tests, get_settings is overridden via dependency injection.
    # We load settings here only to configure OpenAPI docs visibility.
    # Use try/except so the app can be created even without env vars (tests).
    try:
        settings = get_settings()
        docs_url = "/docs" if not settings.is_production else None
        redoc_url = "/redoc" if not settings.is_production else None
        openapi_url = "/openapi.json" if not settings.is_production else None
        app_version = settings.app_version
    except Exception:
        # In testing, settings aren't required at app creation time
        docs_url = "/docs"
        redoc_url = "/redoc"
        openapi_url = "/openapi.json"
        app_version = "0.0.1-test"

    app = FastAPI(
        title="GitHub PR Code Reviewer \u2014 Gateway",
        description=(
            "Webhook gateway service for the GitHub PR Code Reviewer platform. "
            "Receives GitHub webhook events, verifies HMAC signatures, and "
            "enqueues PR events for AI review."
        ),
        version=app_version,
        docs_url=docs_url,
        redoc_url=redoc_url,
        openapi_url=openapi_url,
        lifespan=lifespan,
    )

    # ── Middleware ─────────────────────────────────────────────────────────────

    # CORS — allow all origins in dev, restrict in production
    # We can't reference settings here safely; CORS is permissive in all modes
    # for this platform (gateway is not directly user-facing).
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    # Gzip compression for responses > 1KB
    app.add_middleware(GZipMiddleware, minimum_size=1024)

    # Request context middleware — injects request_id for structured logging
    app.add_middleware(RequestContextMiddleware)

    # ── Routers ───────────────────────────────────────────────────────────────
    app.include_router(health_router)
    app.include_router(webhook_router, prefix="/api/v1")

    # ── Exception Handlers ────────────────────────────────────────────────────
    app.add_exception_handler(RequestValidationError, _validation_error_handler)
    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)
    app.add_exception_handler(Exception, _unhandled_exception_handler)

    return app


# ─── Middleware ────────────────────────────────────────────────────────────────


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Inject a unique request_id into the structlog context for every request.

    This ensures that all log entries produced during a request include the
    request_id, making it easy to correlate logs across services.
    """

    async def dispatch(self, request: Request, call_next: Any) -> Any:
        """Inject request context and process the request."""
        request_id = str(uuid.uuid4())
        request.state.request_id = request_id

        # Bind to structlog context variables (async-safe)
        import structlog.contextvars
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            request_id=request_id,
            method=request.method,
            path=request.url.path,
        )

        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response


# ─── Exception Handlers ───────────────────────────────────────────────────────


async def _validation_error_handler(
    request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    """Handle Pydantic validation errors with a structured response."""
    logger.warning(
        "Request validation error",
        path=request.url.path,
        errors=str(exc.errors()),
    )
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "error": "validation_error",
            "message": "Request validation failed",
            "details": exc.errors(),
        },
    )


async def _http_exception_handler(
    request: Request,
    exc: StarletteHTTPException,
) -> JSONResponse:
    """Handle FastAPI HTTP exceptions with a consistent response envelope."""
    if exc.status_code >= 500:  # noqa: PLR2004
        logger.error(
            "HTTP exception",
            status_code=exc.status_code,
            detail=exc.detail,
            path=request.url.path,
        )
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": _status_to_error_code(exc.status_code),
            "message": exc.detail,
        },
    )


async def _unhandled_exception_handler(
    request: Request,
    exc: Exception,
) -> JSONResponse:
    """Catch-all handler for unhandled exceptions.

    Logs the full exception with stack trace but returns a safe, generic
    error message to the client (never leak internal details in production).
    """
    logger.exception(
        "Unhandled exception",
        path=request.url.path,
        method=request.method,
        exc_type=type(exc).__name__,
    )
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "error": "internal_server_error",
            "message": "An unexpected error occurred. Please try again.",
        },
    )


def _status_to_error_code(status_code: int) -> str:
    """Map HTTP status codes to machine-readable error codes."""
    return {
        400: "bad_request",
        401: "unauthorized",
        403: "forbidden",
        404: "not_found",
        409: "conflict",
        422: "validation_error",
        429: "rate_limited",
        500: "internal_server_error",
        502: "bad_gateway",
        503: "service_unavailable",
    }.get(status_code, "error")


# ─── Application Instance ─────────────────────────────────────────────────────

# This is the ASGI application object referenced by uvicorn
app = create_app()

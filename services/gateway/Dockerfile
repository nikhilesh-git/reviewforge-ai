# =============================================================================
# Gateway Service Dockerfile
# Multi-stage build: development (hot-reload) and production (optimized)
# Build context: repository root (to access shared/ package)
# =============================================================================

# ─── Base stage ──────────────────────────────────────────────────────────────
FROM python:3.13-slim AS base

# Security: run as non-root user
RUN groupadd --gid 1001 appgroup && \
    useradd --uid 1001 --gid appgroup --shell /bin/bash --create-home appuser

# System dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Upgrade pip to latest
RUN pip install --upgrade pip==24.2

WORKDIR /app

# ─── Builder stage ───────────────────────────────────────────────────────────
FROM base AS builder

# Install build tools
RUN pip install build wheel setuptools>=72.0

# Install shared package dependencies first (better layer caching)
COPY shared/pyproject.toml /shared/pyproject.toml
COPY shared/shared/__init__.py /shared/shared/__init__.py
RUN pip install --no-cache-dir /shared/ || true

# Install full shared package
COPY shared/ /shared/
RUN pip install --no-cache-dir /shared/

# Install gateway dependencies
COPY services/gateway/pyproject.toml ./pyproject.toml
COPY services/gateway/app/ ./app/
RUN pip install --no-cache-dir -e ".[dev]"

# ─── Development stage ────────────────────────────────────────────────────────
FROM builder AS development

# Copy alembic config for migrations
COPY alembic.ini ./alembic.ini
COPY migrations/ ./migrations/

# Development: run as root is acceptable for bind mounts
USER root

EXPOSE 8000

CMD ["uvicorn", "app.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--reload", \
     "--log-level", "debug"]

# ─── Production stage ─────────────────────────────────────────────────────────
FROM base AS production

ARG BUILD_VERSION=unknown
ARG BUILD_SHA=unknown

LABEL org.opencontainers.image.title="PR Reviewer Gateway" \
      org.opencontainers.image.description="FastAPI webhook gateway for GitHub PR Code Reviewer" \
      org.opencontainers.image.version="${BUILD_VERSION}" \
      org.opencontainers.image.revision="${BUILD_SHA}"

# Install production dependencies only
COPY shared/ /shared/
RUN pip install --no-cache-dir /shared/

COPY services/gateway/pyproject.toml ./pyproject.toml
COPY services/gateway/app/ ./app/
RUN pip install --no-cache-dir -e . && \
    pip uninstall -y pip setuptools wheel 2>/dev/null || true
COPY alembic.ini ./alembic.ini
COPY migrations/ ./migrations/

# Set build metadata as env vars
ENV BUILD_VERSION=${BUILD_VERSION} \
    BUILD_SHA=${BUILD_SHA} \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONFAULTHANDLER=1

# Switch to non-root user
USER appuser

EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

CMD ["uvicorn", "app.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "2", \
     "--loop", "uvloop", \
     "--http", "httptools", \
     "--proxy-headers", \
     "--forwarded-allow-ips", "*", \
     "--log-level", "info", \
     "--access-log"]

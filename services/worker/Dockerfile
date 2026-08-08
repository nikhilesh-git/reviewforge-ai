# Worker Service Dockerfile (stub — full implementation in Milestone 3)
FROM python:3.13-slim AS base

RUN groupadd --gid 1001 appgroup && \
    useradd --uid 1001 --gid appgroup --shell /bin/bash --create-home appuser

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --upgrade pip==24.2
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu

WORKDIR /app

FROM base AS development
COPY shared/ /shared/
RUN pip install --no-cache-dir /shared/
COPY services/worker/pyproject.toml ./pyproject.toml
COPY services/worker/app/ ./app/
RUN pip install --no-cache-dir -e ".[dev]"
USER appuser
CMD ["celery", "-A", "app.celery_app", "worker", "--loglevel=debug", "--concurrency=2"]

FROM base AS production
ARG BUILD_VERSION=unknown
COPY shared/ /shared/
RUN pip install --no-cache-dir /shared/
COPY services/worker/pyproject.toml ./pyproject.toml
COPY services/worker/app/ ./app/
RUN pip install --no-cache-dir -e .
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 BUILD_VERSION=${BUILD_VERSION}
USER appuser
CMD ["celery", "-A", "app.celery_app", "worker", "--loglevel=info", "--concurrency=4", "--queues=review,priority"]

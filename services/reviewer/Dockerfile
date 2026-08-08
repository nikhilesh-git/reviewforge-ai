# Reviewer Service Dockerfile
FROM python:3.13-slim AS base
RUN groupadd --gid 1001 appgroup && \
    useradd --uid 1001 --gid appgroup --shell /bin/bash --create-home appuser
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --upgrade pip==24.2
WORKDIR /app

FROM base AS development
COPY shared/ /shared/
RUN pip install --no-cache-dir /shared/
COPY services/reviewer/pyproject.toml ./pyproject.toml
COPY services/reviewer/app/ ./app/
RUN pip install --no-cache-dir -e ".[dev]"
USER appuser
CMD ["celery", "-A", "app.celery_app", "worker", "--loglevel=debug", "--queues=review"]

FROM base AS production
COPY shared/ /shared/
RUN pip install --no-cache-dir /shared/
COPY services/reviewer/pyproject.toml ./pyproject.toml
COPY services/reviewer/app/ ./app/
RUN pip install --no-cache-dir -e .
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
USER appuser
CMD ["celery", "-A", "app.celery_app", "worker", "--loglevel=info", "--queues=review"]

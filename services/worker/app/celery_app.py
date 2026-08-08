"""Celery application factory for the worker service.

The Celery app is the central object — shared between the task definitions,
the LangGraph orchestrator, and the stream consumer. All tasks are auto-discovered
from the ``app.tasks`` module.

Task routing:
- ``review`` queue: PR review jobs (CPU/IO intensive, long-running)
- ``priority`` queue: high-priority re-reviews (synchronize events on active PRs)

Signals:
- ``worker_ready``: Initializes DB pool and Redis pool for the worker process.
- ``worker_shutdown``: Gracefully closes all connections.
"""

from __future__ import annotations

import asyncio
import logging

from celery import Celery
from celery.signals import worker_ready, worker_shutdown

from .config import get_settings

logger = logging.getLogger(__name__)


def create_celery_app() -> Celery:
    """Create and configure the Celery application.

    Returns a fully configured Celery instance with:
    - Redis broker and result backend
    - Task autodiscovery
    - Task routing
    - Serialization settings
    """
    settings = get_settings()

    app = Celery(
        "pr_reviewer_worker",
        broker=settings.celery_broker_url,
        backend=settings.celery_result_backend,
        include=["app.tasks"],
    )

    app.conf.update(
        # Serialization — always use JSON for interoperability
        task_serializer="json",
        accept_content=["json"],
        result_serializer="json",
        # Timezone
        timezone="UTC",
        enable_utc=True,
        # Task result settings
        task_track_started=True,
        task_acks_late=True,               # Acknowledge AFTER task completes (reliability)
        task_reject_on_worker_lost=True,   # Re-queue if worker dies mid-task
        # Routing
        task_routes={
            "app.tasks.review_pr": {
                "queue": "review",
                "routing_key": "review.pr",
            },
        },
        task_default_queue="review",
        task_queues={
            "review": {"exchange": "review", "routing_key": "review.#"},
            "priority": {"exchange": "priority", "routing_key": "priority.#"},
        },
        # Worker configuration
        worker_prefetch_multiplier=1,      # One task at a time per worker (LLM calls are slow)
        task_soft_time_limit=480,          # 8 minutes soft limit (LangGraph timeout)
        task_time_limit=600,               # 10 minutes hard kill limit
        # Result expiry
        result_expires=3600,               # Keep results for 1 hour
        # Concurrency
        worker_max_tasks_per_child=50,     # Restart worker after 50 tasks (memory hygiene)
    )

    return app


# Module-level Celery instance (referenced by Dockerfile CMD)
celery_app = create_celery_app()


# ─── Worker Lifecycle Signals ─────────────────────────────────────────────────


@worker_ready.connect
def on_worker_ready(sender: object, **kwargs: object) -> None:
    """Initialize shared resources when a Celery worker process starts.

    This runs in each worker process (not the main process), so we must
    initialize per-process resources here (DB pool, Redis pool).
    """
    from shared.infrastructure.database import init_database
    from shared.infrastructure.logging import configure_logging
    from shared.infrastructure.redis_client import init_redis

    settings = get_settings()

    configure_logging(
        log_level=settings.log_level,
        json_output=settings.is_production,
        service_name="pr-review-worker",
    )

    logger.info("Worker process starting — initializing shared resources")

    init_database(database_url=settings.database_url, echo=settings.debug)
    init_redis(redis_url=settings.redis_url, max_connections=20)

    logger.info("Worker process ready — shared resources initialized")


@worker_shutdown.connect
def on_worker_shutdown(sender: object, **kwargs: object) -> None:
    """Close shared resources gracefully on worker shutdown."""
    from shared.infrastructure.redis_client import close_redis
    from shared.infrastructure.database import close_database

    logger.info("Worker process shutting down — closing shared resources")

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(close_redis())
        loop.run_until_complete(close_database())
    finally:
        loop.close()

    logger.info("Worker process shutdown complete")

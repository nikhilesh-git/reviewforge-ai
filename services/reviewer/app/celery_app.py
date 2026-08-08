"""Celery application factory for the reviewer service."""

from __future__ import annotations

import asyncio
import logging

from celery import Celery
from celery.signals import worker_ready, worker_shutdown

from .config import get_settings

logger = logging.getLogger(__name__)


def create_celery_app() -> Celery:
    """Create and configure the reviewer Celery application."""
    settings = get_settings()

    app = Celery(
        "pr_reviewer_reviewer",
        broker=settings.celery_broker_url,
        backend=settings.celery_result_backend,
        include=["app.tasks"],
    )

    app.conf.update(
        task_serializer="json",
        accept_content=["json"],
        result_serializer="json",
        timezone="UTC",
        enable_utc=True,
        task_track_started=True,
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        task_routes={
            "app.tasks.post_review_comments": {
                "queue": "review",
                "routing_key": "review.post",
            },
        },
        task_default_queue="review",
        worker_prefetch_multiplier=2,
        task_soft_time_limit=120,
        task_time_limit=180,
        result_expires=3600,
    )

    return app


celery_app = create_celery_app()


@worker_ready.connect
def on_worker_ready(sender: object, **kwargs: object) -> None:
    """Initialize shared resources when a reviewer worker process starts."""
    from shared.infrastructure.database import init_database
    from shared.infrastructure.logging import configure_logging

    settings = get_settings()
    configure_logging(
        log_level=settings.log_level,
        json_output=settings.is_production,
        service_name="pr-review-reviewer",
    )
    init_database(database_url=settings.database_url, echo=settings.debug)
    logger.info("Reviewer worker process ready")


@worker_shutdown.connect
def on_worker_shutdown(sender: object, **kwargs: object) -> None:
    """Close shared resources gracefully on worker shutdown."""
    from shared.infrastructure.database import close_database

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(close_database())
    finally:
        loop.close()
    logger.info("Reviewer worker shutdown complete")

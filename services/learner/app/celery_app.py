"""Celery application factory for the learner service."""

from __future__ import annotations

import logging

from celery import Celery
from celery.signals import worker_ready

from .config import get_settings

logger = logging.getLogger(__name__)


def create_celery_app() -> Celery:
    """Create and configure the learner Celery application."""
    settings = get_settings()

    app = Celery(
        "pr_reviewer_learner",
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
        task_acks_late=True,
        task_routes={
            "app.tasks.learn_from_pr": {
                "queue": "learn",
                "routing_key": "learn.pr",
            },
        },
        task_default_queue="learn",
        worker_prefetch_multiplier=1,
        task_soft_time_limit=240,
        task_time_limit=300,
        result_expires=3600,
    )

    return app


celery_app = create_celery_app()


@worker_ready.connect
def on_worker_ready(sender: object, **kwargs: object) -> None:
    """Initialize shared resources when a learner worker process starts."""
    from shared.infrastructure.logging import configure_logging

    settings = get_settings()
    configure_logging(
        log_level=settings.log_level,
        json_output=settings.is_production,
        service_name="pr-review-learner",
    )
    logger.info("Learner worker process ready")

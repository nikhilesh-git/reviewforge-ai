"""Structured logging configuration using structlog.

Why structlog?
- JSON output in production for log aggregation (Loki, ELK, etc.)
- Human-readable colored output in development
- Context binding (request_id, repo, pr_number automatically included)
- Async-compatible (no thread-local state issues)
- Zero-cost when log level is disabled (lazy evaluation)

Usage::

    import structlog
    logger = structlog.get_logger(__name__)

    # Simple log
    logger.info("Processing webhook", delivery_id="abc-123", event="pull_request")

    # Bind context for all subsequent log entries in this scope
    log = logger.bind(repo="owner/repo", pr_number=42)
    log.info("Starting review")
    log.warning("Agent timeout", agent="security", timeout_s=30)
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog


def configure_logging(
    log_level: str = "INFO",
    *,
    json_output: bool = True,
    service_name: str = "pr-reviewer",
) -> None:
    """Configure structlog for the application.

    Args:
        log_level: Python logging level string (DEBUG, INFO, WARNING, ERROR).
        json_output: If True, emit JSON logs (for production/log aggregation).
                     If False, emit human-readable colored logs (for development).
        service_name: Service name added to every log entry.
    """
    log_level_int = getattr(logging, log_level.upper(), logging.INFO)

    # Configure stdlib logging to route through structlog
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=log_level_int,
    )

    # Silence noisy third-party loggers
    for noisy_logger in [
        "uvicorn.access",
        "sqlalchemy.engine",
        "aiohttp",
        "httpx",
        "httpcore",
    ]:
        logging.getLogger(noisy_logger).setLevel(logging.WARNING)

    # Shared processors (applied to all log entries)
    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.CallsiteParameterAdder(
            [
                structlog.processors.CallsiteParameter.FUNC_NAME,
                structlog.processors.CallsiteParameter.LINENO,
            ]
        ),
        _add_service_name(service_name),
    ]

    if json_output:
        # Production: clean JSON for log aggregation
        processors = [
            *shared_processors,
            structlog.processors.dict_tracebacks,
            structlog.processors.JSONRenderer(),
        ]
    else:
        # Development: pretty colored output
        processors = [
            *shared_processors,
            structlog.dev.ConsoleRenderer(colors=True),
        ]

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def _add_service_name(service_name: str) -> Any:
    """Structlog processor that injects the service name into every log entry."""

    def processor(
        logger: Any,  # noqa: ANN401
        method: str,
        event_dict: dict[str, Any],
    ) -> dict[str, Any]:
        event_dict["service"] = service_name
        return event_dict

    return processor


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a structlog bound logger for the given module name."""
    return structlog.get_logger(name)


def bind_request_context(
    request_id: str,
    *,
    delivery_id: str | None = None,
    repo: str | None = None,
    pr_number: int | None = None,
) -> None:
    """Bind request context variables for structured logging.

    These values will be included in all log entries produced during
    the current async task or request. Call this at the start of a
    request handler or Celery task.

    Args:
        request_id: Internal request ID (UUID).
        delivery_id: GitHub X-GitHub-Delivery ID.
        repo: Repository full name (owner/repo).
        pr_number: Pull request number.
    """
    context: dict[str, Any] = {"request_id": request_id}
    if delivery_id:
        context["delivery_id"] = delivery_id
    if repo:
        context["repo"] = repo
    if pr_number is not None:
        context["pr_number"] = pr_number

    structlog.contextvars.bind_contextvars(**context)


def clear_request_context() -> None:
    """Clear all bound context variables (call at end of request/task)."""
    structlog.contextvars.clear_contextvars()

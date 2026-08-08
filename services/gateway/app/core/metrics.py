"""Gateway-specific Prometheus metrics.

These metrics complement the shared metrics in ``shared.infrastructure.metrics``.
The gateway exports all metrics on ``GET /metrics`` using the standard
Prometheus text exposition format via ``prometheus_client.generate_latest()``.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from shared.infrastructure.metrics import (
    GATEWAY_ACTIVE_CONNECTIONS,
    GATEWAY_EVENTS_DEDUPLICATED_TOTAL,
    GATEWAY_EVENTS_PUBLISHED_TOTAL,
    GATEWAY_HMAC_FAILURES_TOTAL,
    GATEWAY_WEBHOOK_DURATION_SECONDS,
    GATEWAY_WEBHOOK_REQUESTS_TOTAL,
)

__all__ = [
    "CONTENT_TYPE_LATEST",
    "GATEWAY_ACTIVE_CONNECTIONS",
    "GATEWAY_EVENTS_DEDUPLICATED_TOTAL",
    "GATEWAY_EVENTS_PUBLISHED_TOTAL",
    "GATEWAY_HMAC_FAILURES_TOTAL",
    "GATEWAY_WEBHOOK_DURATION_SECONDS",
    "GATEWAY_WEBHOOK_REQUESTS_TOTAL",
    "generate_latest",
    "record_webhook_received",
    "record_event_published",
    "record_hmac_failure",
    "record_deduplication",
]


def record_webhook_received(
    event_type: str,
    action: str,
    status: str,
    duration_seconds: float,
) -> None:
    """Record metrics for a completed webhook processing cycle.

    Args:
        event_type: GitHub event type (e.g. "pull_request").
        action: PR action (e.g. "opened") or "unknown".
        status: Processing outcome:
                "accepted" | "rejected_hmac" | "rejected_event_type" | "error"
        duration_seconds: Total processing time.
    """
    GATEWAY_WEBHOOK_REQUESTS_TOTAL.labels(
        event_type=event_type,
        action=action,
        status=status,
    ).inc()
    GATEWAY_WEBHOOK_DURATION_SECONDS.observe(duration_seconds)


def record_event_published(repo_full_name: str) -> None:
    """Record a successfully published event.

    Args:
        repo_full_name: Repository in owner/repo format.
    """
    # Use "unknown" for repos not in our allowlist to keep cardinality low
    safe_repo = repo_full_name if "/" in repo_full_name else "unknown/unknown"
    GATEWAY_EVENTS_PUBLISHED_TOTAL.labels(repo=safe_repo).inc()


def record_hmac_failure() -> None:
    """Increment HMAC verification failure counter."""
    GATEWAY_HMAC_FAILURES_TOTAL.inc()


def record_deduplication() -> None:
    """Increment deduplication counter."""
    GATEWAY_EVENTS_DEDUPLICATED_TOTAL.inc()


class RequestTimer:
    """Context manager that times a block and records the duration.

    Example::

        with RequestTimer() as timer:
            await do_work()
        record_webhook_received(..., duration_seconds=timer.elapsed)
    """

    def __init__(self) -> None:
        self._start: float = 0.0
        self.elapsed: float = 0.0

    def __enter__(self) -> "RequestTimer":
        self._start = time.perf_counter()
        return self

    def __exit__(self, *args: Any) -> None:
        self.elapsed = time.perf_counter() - self._start

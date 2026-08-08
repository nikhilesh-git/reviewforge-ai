"""GitHub webhook receiver endpoint.

This is the most critical endpoint in the gateway — it must:
1. Accept the raw request body BEFORE FastAPI parses it (required for HMAC)
2. Verify the HMAC-SHA256 signature in constant time
3. Validate the event type and action
4. Deduplicate identical events
5. Persist the event record
6. Publish to Redis stream
7. Return 202 Accepted quickly (< 10s SLA from GitHub)

Error handling philosophy:
- HMAC failure → 401 Unauthorized (never reveal details)
- Unknown event type → 200 OK (don't fail GitHub's delivery)
- Non-reviewable action → 200 OK (processed but ignored)
- Internal error → 500, but log everything
- Duplicate event → 200 OK (idempotent)

Reference: https://docs.github.com/en/webhooks/using-webhooks/best-practices-for-using-webhooks
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone

UTC = timezone.utc
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from shared.domain.enums import PRAction
from shared.domain.events import PRLearnRequestedEvent, PRReviewRequestedEvent
from shared.infrastructure.redis_client import stream_publish

from ..core.config import Settings
from ..core.dependencies import DBSessionDep, RedisDep, SettingsDep
from ..core.metrics import (
    RequestTimer,
    record_deduplication,
    record_event_published,
    record_hmac_failure,
    record_webhook_received,
)
from ..core.security import verify_github_signature
from ..repositories.event_repository import DuplicateEventError, EventRepository

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/webhooks", tags=["Webhooks"])

# GitHub event types we're interested in
_HANDLED_EVENT_TYPES = frozenset({"pull_request"})

# PR actions that trigger a review
_REVIEWABLE_ACTIONS = PRAction.reviewable()


# ─── Response Models ──────────────────────────────────────────────────────────


class WebhookAcceptedResponse(BaseModel):
    """Response body returned for successfully accepted webhook events."""

    status: str = "accepted"
    job_id: str
    delivery_id: str
    message: str = "PR review job enqueued"


class WebhookIgnoredResponse(BaseModel):
    """Response body returned for valid but ignored webhook events."""

    status: str = "ignored"
    delivery_id: str
    reason: str


# ─── Webhook Handler ─────────────────────────────────────────────────────────


@router.post(
    "/github",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=WebhookAcceptedResponse,
    responses={
        200: {"description": "Event received but ignored (non-reviewable action or event type)"},
        401: {"description": "HMAC signature verification failed"},
        422: {"description": "Malformed webhook payload"},
        500: {"description": "Internal processing error"},
    },
    summary="Receive GitHub Pull Request Webhook",
    description=(
        "Receives GitHub webhook events, verifies HMAC-SHA256 signatures, "
        "and enqueues pull_request events for AI review."
    ),
)
async def receive_github_webhook(
    request: Request,
    redis: RedisDep,
    db: DBSessionDep,
    settings: SettingsDep,
    x_hub_signature_256: Annotated[str | None, Header()] = None,
    x_github_event: Annotated[str | None, Header()] = None,
    x_github_delivery: Annotated[str | None, Header()] = None,
) -> JSONResponse:
    """Main GitHub webhook receiver.

    All GitHub webhook deliveries arrive here. The handler must return
    within 10 seconds or GitHub will consider the delivery failed.

    The handler:
    1. Reads raw bytes (required for HMAC before JSON parse)
    2. Verifies HMAC-SHA256 signature
    3. Filters for pull_request events with reviewable actions
    4. Deduplicates (same sha + action already queued)
    5. Persists event record to PostgreSQL
    6. Publishes PRReviewRequestedEvent to Redis stream
    7. Returns 202 Accepted with job_id
    """
    delivery_id = x_github_delivery or str(uuid.uuid4())
    event_type = x_github_event or "unknown"

    log = logger.bind(
        delivery_id=delivery_id,
        event_type=event_type,
    )

    with RequestTimer() as timer:
        # ── Step 1: Read raw body (must happen before any parsing) ───────────
        raw_body = await request.body()

        # ── Step 2: Verify HMAC signature ────────────────────────────────────
        if not verify_github_signature(raw_body, x_hub_signature_256, settings.github_webhook_secret):
            record_hmac_failure()
            record_webhook_received(event_type, "unknown", "rejected_hmac", timer.elapsed)
            log.warning("Webhook rejected: HMAC signature invalid")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Signature verification failed",
            )

        # ── Step 3: Parse JSON payload ────────────────────────────────────────
        try:
            payload: dict[str, Any] = json.loads(raw_body)
        except json.JSONDecodeError as exc:
            record_webhook_received(event_type, "unknown", "error", timer.elapsed)
            log.error("Failed to parse webhook payload", error=str(exc))
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Invalid JSON payload",
            ) from exc

        action = payload.get("action", "unknown")
        log = log.bind(action=action)

        # ── Step 4: Filter event type ─────────────────────────────────────────
        if event_type not in _HANDLED_EVENT_TYPES:
            record_webhook_received(event_type, action, "ignored_event_type", timer.elapsed)
            log.debug("Ignoring non-PR event")
            return JSONResponse(
                status_code=status.HTTP_200_OK,
                content=WebhookIgnoredResponse(
                    delivery_id=delivery_id,
                    reason=f"Event type '{event_type}' is not processed by this platform",
                ).model_dump(),
            )

        # ── Step 5: Extract PR data ───────────────────────────────────────────
        pr_data = payload.get("pull_request", {})
        repo_data = payload.get("repository", {})
        sender_data = payload.get("sender", {})
        installation_data = payload.get("installation", {})

        repo_full_name: str = repo_data.get("full_name", "unknown/unknown")
        pr_number: int = pr_data.get("number", 0)
        pr_title: str = pr_data.get("title", "")
        pr_url: str = pr_data.get("html_url", "")
        author_login: str = sender_data.get("login", "unknown")
        installation_id: int | None = installation_data.get("id") if installation_data else None

        head_data = pr_data.get("head", {})
        base_data = pr_data.get("base", {})
        head_sha: str = head_data.get("sha", "")
        base_sha: str = base_data.get("sha", "")

        log = log.bind(repo=repo_full_name, pr_number=pr_number)

        # ── Step 6: Filter reviewable actions ─────────────────────────────────
        if action not in _REVIEWABLE_ACTIONS:
            # Handle merged PRs separately — trigger learner service
            if action == "closed" and pr_data.get("merged"):
                await _handle_pr_merged(
                    redis=redis,
                    settings=settings,
                    repo_full_name=repo_full_name,
                    pr_number=pr_number,
                    head_sha=head_sha,
                    installation_id=installation_id,
                    log=log,
                )

            event_repo = EventRepository(db)
            await event_repo.mark_ignored(
                delivery_id,
                reason=f"Action '{action}' is not reviewable",
            )

            record_webhook_received(event_type, action, "ignored_action", timer.elapsed)
            log.debug("Ignoring non-reviewable action", action=action)
            return JSONResponse(
                status_code=status.HTTP_200_OK,
                content=WebhookIgnoredResponse(
                    delivery_id=delivery_id,
                    reason=f"PR action '{action}' does not trigger a review",
                ).model_dump(),
            )

        # ── Step 7: Build unique deduplication key ─────────────────────────────
        unique_key = f"{repo_full_name}#{pr_number}@{head_sha}:{action}"

        # ── Step 8: Persist event record ──────────────────────────────────────
        event_repo = EventRepository(db)
        try:
            await event_repo.save_received(
                delivery_id=delivery_id,
                event_type=event_type,
                action=action,
                repository_full_name=repo_full_name,
                pr_number=pr_number,
                head_sha=head_sha,
                base_sha=base_sha,
                author_login=author_login,
                installation_id=installation_id,
                unique_key=unique_key,
                raw_payload=payload,
            )
        except DuplicateEventError:
            record_deduplication()
            record_webhook_received(event_type, action, "deduplicated", timer.elapsed)
            log.info("Duplicate event detected — already queued", unique_key=unique_key)
            return JSONResponse(
                status_code=status.HTTP_200_OK,
                content=WebhookIgnoredResponse(
                    delivery_id=delivery_id,
                    reason="This PR event has already been queued for review",
                ).model_dump(),
            )

        # ── Step 9: Assign job ID and build stream event ───────────────────────
        job_id = str(uuid.uuid4())

        review_event = PRReviewRequestedEvent(
            delivery_id=delivery_id,
            job_id=job_id,
            repo_full_name=repo_full_name,
            pr_number=pr_number,
            pr_title=pr_title,
            action=PRAction(action),
            head_sha=head_sha,
            base_sha=base_sha,
            pr_url=pr_url,
            author_login=author_login,
            installation_id=installation_id,
        )

        # ── Step 10: Publish to Redis Stream ──────────────────────────────────
        try:
            stream_entry_id = await stream_publish(
                client=redis,
                stream_name=settings.redis_stream_name,
                fields=review_event.to_redis_dict(),
                max_length=settings.redis_max_stream_length,
            )
        except Exception as exc:
            await event_repo.mark_failed(delivery_id, error_message=str(exc))
            record_webhook_received(event_type, action, "error", timer.elapsed)
            log.error("Failed to publish event to Redis stream", error=str(exc))
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to enqueue review job",
            ) from exc

        # ── Step 11: Mark event as queued ─────────────────────────────────────
        await event_repo.mark_queued(delivery_id, stream_entry_id=stream_entry_id)

        # ── Step 12: Record metrics and respond ───────────────────────────────
        record_event_published(repo_full_name)

    record_webhook_received(event_type, action, "accepted", timer.elapsed)

    log.info(
        "Webhook accepted and review job enqueued",
        job_id=job_id,
        stream_entry_id=stream_entry_id,
        duration_ms=round(timer.elapsed * 1000, 2),
    )

    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content=WebhookAcceptedResponse(
            job_id=job_id,
            delivery_id=delivery_id,
        ).model_dump(),
    )


# ─── Merged PR Handler ────────────────────────────────────────────────────────


async def _handle_pr_merged(
    *,
    redis: Any,
    settings: Settings,
    repo_full_name: str,
    pr_number: int,
    head_sha: str,
    installation_id: int | None,
    log: Any,
) -> None:
    """Publish a learn event when a PR is merged.

    The Learner service consumes this event to extract repository conventions.
    This is a best-effort fire-and-forget — if it fails, we log the error
    but don't fail the webhook response.
    """
    learn_stream = f"{settings.redis_stream_name}:learn"
    learn_event = PRLearnRequestedEvent(
        repo_full_name=repo_full_name,
        pr_number=pr_number,
        head_sha=head_sha,
        merged_at=datetime.now(UTC),
        installation_id=installation_id,
    )
    try:
        await stream_publish(
            client=redis,
            stream_name=learn_stream,
            fields=learn_event.to_redis_dict(),
        )
        log.info("Published learn event for merged PR")
    except Exception as exc:
        log.warning("Failed to publish learn event (non-critical)", error=str(exc))

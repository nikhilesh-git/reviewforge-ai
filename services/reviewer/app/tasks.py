"""Celery tasks for the reviewer service.

The ``post_review_comments`` task receives the merged review findings
from the worker and posts them as GitHub PR review comments.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone

import structlog
from celery import Task

from .celery_app import celery_app
from .config import get_settings

UTC = timezone.utc
logger = structlog.get_logger(__name__)


def _run_async(coro):
    """Run an async coroutine from a sync Celery task."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@celery_app.task(
    name="app.tasks.post_review_comments",
    bind=True,
    max_retries=3,
    default_retry_delay=30,
    queue="review",
    acks_late=True,
)
def post_review_comments(self: Task, payload: dict) -> dict:
    """Post AI review findings as GitHub PR review comments.

    Args:
        self: The Celery task instance.
        payload: Dict containing job_id, repo_full_name, pr_number, head_sha,
                 agent_results, and merged_findings (serialized).

    Returns:
        Dict with review_id and comments_posted count.
    """
    job_id = payload.get("job_id", "unknown")
    repo_full_name = payload.get("repo_full_name", "")
    pr_number = payload.get("pr_number", 0)

    log = logger.bind(
        job_id=job_id,
        repo=repo_full_name,
        pr_number=pr_number,
    )

    log.info("Starting post_review_comments task")

    try:
        result = _run_async(_post_comments(payload=payload, log=log))
        log.info(
            "Review comments posted successfully",
            review_id=result.get("review_id"),
            comments=result.get("comments_posted", 0),
        )
        return result

    except Exception as exc:
        log.error("Failed to post review comments", error=str(exc))

        if self.request.retries < self.max_retries:
            log.warning(
                "Retrying post_review_comments task",
                retry=self.request.retries + 1,
            )
            raise self.retry(exc=exc)

        return {
            "status": "failed",
            "error": str(exc),
            "job_id": job_id,
            "comments_posted": 0,
        }


async def _post_comments(*, payload: dict, log) -> dict:
    """Async implementation of the comment posting pipeline."""
    from shared.domain.models import AgentResult, ReviewFinding
    from shared.domain.enums import Severity
    from shared.infrastructure.database import get_db_session
    from shared.infrastructure.orm_models import ReviewJobRecord
    from sqlalchemy import update

    from .comment_formatter import format_finding_comment, format_review_summary
    from .github_publisher import create_github_publisher

    settings = get_settings()

    # Parse domain objects from the serialized payload
    agent_results = [
        AgentResult.model_validate(r)
        for r in payload.get("agent_results", [])
    ]
    merged_findings = [
        ReviewFinding.model_validate(f)
        for f in payload.get("merged_findings", [])
    ]

    job_id = payload.get("job_id")
    repo_full_name = payload.get("repo_full_name", "")
    pr_number = payload.get("pr_number", 0)
    head_sha = payload.get("head_sha", "")

    # Filter findings by minimum severity
    severity_order = {
        Severity.CRITICAL: 0,
        Severity.HIGH: 1,
        Severity.MEDIUM: 2,
        Severity.LOW: 3,
        Severity.INFO: 4,
    }
    min_sev = settings.min_severity_to_post.lower()
    min_sev_value = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}.get(min_sev, 3)

    filtered_findings = [
        f for f in merged_findings
        if severity_order.get(f.severity, 4) <= min_sev_value
    ]

    # Cap at max_inline_comments
    inline_findings = filtered_findings[:settings.max_inline_comments]

    log.info(
        "Formatting comments",
        total_findings=len(merged_findings),
        filtered_findings=len(filtered_findings),
        inline_comments=len(inline_findings),
    )

    # Format the review summary
    review_body, review_event = format_review_summary(
        agent_results=agent_results,
        merged_findings=merged_findings,
        repo_full_name=repo_full_name,
        pr_number=pr_number,
    )

    # Format inline comments
    inline_comments = []
    for finding in inline_findings:
        formatted = format_finding_comment(finding)
        if formatted.is_inline:
            inline_comments.append({
                "path": formatted.path,
                "line": formatted.line,
                "side": formatted.side,
                "body": formatted.body,
            })
        # PR-level findings are already captured in the summary

    # Create the GitHub publisher
    publisher = create_github_publisher(pat=settings.github_pat)

    review_id = await publisher.create_review(
        repo_full_name=repo_full_name,
        pr_number=pr_number,
        head_sha=head_sha,
        body=review_body,
        event=review_event,
        inline_comments=inline_comments,
    )

    # Update DB with the GitHub review ID
    if job_id:
        try:
            async with get_db_session() as session:
                await session.execute(
                    update(ReviewJobRecord)
                    .where(ReviewJobRecord.id == uuid.UUID(job_id))
                    .values(
                        github_review_id=review_id,
                        github_comments_posted=len(inline_comments),
                    )
                )
        except Exception as exc:
            log.warning("Failed to update review job with GitHub review ID", error=str(exc))

    return {
        "status": "completed",
        "job_id": job_id,
        "review_id": review_id,
        "comments_posted": len(inline_comments),
        "review_event": review_event,
    }

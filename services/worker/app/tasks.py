"""Celery tasks for the worker service.

This module defines the ``review_pr`` task — the core of the platform.

Task lifecycle:
1. Receive ``PRReviewRequestedEvent`` payload from Redis stream
2. Update job status to FETCHING_DIFF in PostgreSQL
3. Build and run the LangGraph review pipeline
4. Update job status to PUBLISHING with merged findings
5. Dispatch ``post_review_comments`` task to the reviewer service
6. Update job status to COMPLETED

Error handling:
- Each status update stage is wrapped in try/except
- Failures update the DB record with the error message
- Celery's ``max_retries`` handles transient failures
- Langfuse traces the entire job for debugging
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

# Module-level orchestrator (initialized lazily on first use)
_orchestrator = None


def _get_orchestrator():
    """Lazily initialize the ReviewOrchestrator singleton.

    Using a global here avoids re-creating LangChain clients on every task.
    The orchestrator is thread-safe once created.
    """
    global _orchestrator  # noqa: PLW0603
    if _orchestrator is None:
        from .graph import ReviewOrchestrator
        settings = get_settings()
        _orchestrator = ReviewOrchestrator(settings)
    return _orchestrator


def _run_async(coro):
    """Run an async coroutine from a synchronous Celery task.

    Celery workers are synchronous by default. We create a new event loop
    for each task execution to avoid sharing loop state between tasks.
    """
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@celery_app.task(
    name="app.tasks.review_pr",
    bind=True,
    max_retries=2,
    default_retry_delay=60,
    queue="review",
    track_started=True,
    acks_late=True,
)
def review_pr(self: Task, event_data: dict) -> dict:
    """Main Celery task: orchestrate a full PR review.

    Args:
        self: The Celery task instance (bound task).
        event_data: Serialized ``PRReviewRequestedEvent`` dictionary.

    Returns:
        A summary dict with job_id, findings count, and status.
    """
    from shared.domain.events import PRReviewRequestedEvent
    from shared.infrastructure.metrics import (
        WORKER_ACTIVE_JOBS,
        WORKER_JOB_DURATION_SECONDS,
        WORKER_REVIEW_JOBS_TOTAL,
    )
    import time

    # Parse the incoming event
    event = PRReviewRequestedEvent.model_validate(event_data)
    job_id = event.job_id

    log = logger.bind(
        job_id=job_id,
        repo=event.repo_full_name,
        pr_number=event.pr_number,
        action=event.action,
    )

    log.info("Starting PR review task", celery_task_id=self.request.id)

    WORKER_ACTIVE_JOBS.inc()
    start_time = time.perf_counter()

    try:
        result = _run_async(
            _execute_review(
                event=event,
                job_id=job_id,
                celery_task_id=self.request.id,
                log=log,
            )
        )

        duration = time.perf_counter() - start_time
        WORKER_JOB_DURATION_SECONDS.observe(duration)
        WORKER_REVIEW_JOBS_TOTAL.labels(
            action=event.action.value,
            status="completed",
        ).inc()

        log.info(
            "PR review task completed",
            findings=result.get("findings_count", 0),
            duration_seconds=round(duration, 2),
        )

        return result

    except Exception as exc:
        duration = time.perf_counter() - start_time
        WORKER_REVIEW_JOBS_TOTAL.labels(
            action=event.action.value,
            status="failed",
        ).inc()

        log.error(
            "PR review task failed",
            error=str(exc),
            duration_seconds=round(duration, 2),
        )

        # Retry on transient errors
        if self.request.retries < self.max_retries:
            log.warning(
                "Retrying task",
                retry=self.request.retries + 1,
                max_retries=self.max_retries,
            )
            raise self.retry(exc=exc)

        # Mark job as failed in DB
        _run_async(_mark_job_failed(job_id, str(exc)))
        raise

    finally:
        WORKER_ACTIVE_JOBS.dec()


async def _execute_review(
    *,
    event,
    job_id: str,
    celery_task_id: str,
    log,
) -> dict:
    """Async implementation of the review pipeline.

    Separated from the sync Celery task to allow clean async/await usage.
    """
    from shared.domain.enums import JobStatus
    from shared.infrastructure.database import get_db_session
    from shared.infrastructure.orm_models import ReviewJobRecord, PREventRecord
    from sqlalchemy import select, update

    # ── Step 1: Update job status to FETCHING_DIFF ─────────────────────────
    async with get_db_session() as session:
        # Find the PR event record
        stmt = select(PREventRecord).where(
            PREventRecord.delivery_id == event.delivery_id
        )
        result = await session.execute(stmt)
        pr_event_record = result.scalar_one_or_none()

        if pr_event_record is None:
            log.warning("PR event record not found in DB", delivery_id=event.delivery_id)

        # Use pr_event_record.id if found, otherwise generate a placeholder
        pr_event_id = pr_event_record.id if pr_event_record is not None else uuid.uuid4()

        # Create or update the review job record
        job_record = ReviewJobRecord(
            id=uuid.UUID(job_id),
            pr_event_id=pr_event_id,
            celery_task_id=celery_task_id,
            status=JobStatus.FETCHING_DIFF,
            priority=event.priority,
            started_at=datetime.now(UTC),
        )
        session.add(job_record)

    log.info("Starting LangGraph review pipeline")

    # ── Step 2: Update status to REVIEWING ─────────────────────────────────
    async with get_db_session() as session:
        await session.execute(
            update(ReviewJobRecord)
            .where(ReviewJobRecord.id == uuid.UUID(job_id))
            .values(status=JobStatus.REVIEWING)
        )

    # ── Step 3: Run the LangGraph review pipeline ──────────────────────────
    orchestrator = _get_orchestrator()
    agent_results, merged_findings = await orchestrator.run(
        repo_full_name=event.repo_full_name,
        pr_number=event.pr_number,
        head_sha=event.head_sha,
        base_sha=event.base_sha,
        author_login=event.author_login,
        installation_id=event.installation_id,
    )

    # ── Step 4: Persist results to DB ──────────────────────────────────────
    total_tokens = sum(r.tokens_used for r in agent_results)
    critical_count = sum(
        1 for f in merged_findings
        if f.severity.value in ("critical", "high")
    )

    async with get_db_session() as session:
        await session.execute(
            update(ReviewJobRecord)
            .where(ReviewJobRecord.id == uuid.UUID(job_id))
            .values(
                status=JobStatus.PUBLISHING,
                agent_results=[r.model_dump() for r in agent_results],
                findings=[f.model_dump() for f in merged_findings],
                total_findings=len(merged_findings),
                critical_findings=critical_count,
                tokens_used=total_tokens,
            )
        )

    # ── Step 5: Dispatch reviewer task ─────────────────────────────────────
    if merged_findings or agent_results:
        post_review_comments.apply_async(
            args=[{
                "job_id": job_id,
                "repo_full_name": event.repo_full_name,
                "pr_number": event.pr_number,
                "pr_url": event.pr_url,
                "head_sha": event.head_sha,
                "installation_id": event.installation_id,
                "agent_results": [r.model_dump() for r in agent_results],
                "merged_findings": [f.model_dump() for f in merged_findings],
            }],
            queue="review",
        )
        log.info(
            "Dispatched reviewer task",
            findings=len(merged_findings),
            critical=critical_count,
        )

    # ── Step 6: Mark as completed ───────────────────────────────────────────
    async with get_db_session() as session:
        await session.execute(
            update(ReviewJobRecord)
            .where(ReviewJobRecord.id == uuid.UUID(job_id))
            .values(
                status=JobStatus.COMPLETED,
                completed_at=datetime.now(UTC),
            )
        )

    return {
        "job_id": job_id,
        "findings_count": len(merged_findings),
        "critical_count": critical_count,
        "tokens_used": total_tokens,
        "agents_ran": len(agent_results),
        "status": "completed",
    }


async def _mark_job_failed(job_id: str, error_message: str) -> None:
    """Mark a review job as failed in the database."""
    from shared.domain.enums import JobStatus
    from shared.infrastructure.database import get_db_session
    from shared.infrastructure.orm_models import ReviewJobRecord
    from sqlalchemy import update

    try:
        async with get_db_session() as session:
            await session.execute(
                update(ReviewJobRecord)
                .where(ReviewJobRecord.id == uuid.UUID(job_id))
                .values(
                    status=JobStatus.FAILED,
                    error_message=error_message[:2000],
                    completed_at=datetime.now(UTC),
                )
            )
    except Exception as exc:
        logger.error("Failed to update job status to failed", error=str(exc))


# Import reviewer task for dispatching (circular-safe via string name)
# This allows the worker to send tasks to the reviewer queue.
@celery_app.task(name="app.tasks.post_review_comments")
def post_review_comments(payload: dict) -> dict:
    """Stub task that forwards to the reviewer service.

    In production, the reviewer service runs its own Celery worker
    and picks up tasks from the same Redis broker.
    """
    logger.info(
        "post_review_comments task received — reviewer service will handle this",
        job_id=payload.get("job_id"),
        findings=len(payload.get("merged_findings", [])),
    )
    return {"status": "forwarded"}

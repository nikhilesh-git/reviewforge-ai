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
                agent_results=[r.model_dump(mode="json") for r in agent_results],
                findings=[f.model_dump(mode="json") for f in merged_findings],
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
                "agent_results": [r.model_dump(mode="json") for r in agent_results],
                "merged_findings": [f.model_dump(mode="json") for f in merged_findings],
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


# ─── GitHub Comment Posting ───────────────────────────────────────────────────


@celery_app.task(
    name="app.tasks.post_review_comments",
    bind=True,
    max_retries=3,
    default_retry_delay=30,
    queue="review",
)
def post_review_comments(self, payload: dict) -> dict:
    """Post the AI review findings as a GitHub PR review comment.

    Formats the merged findings from all agents into a clean, structured
    markdown comment and submits it as a PR review via the GitHub API.
    """
    return _run_async(_post_review_to_github(self, payload))


async def _post_review_to_github(task, payload: dict) -> dict:
    """Async implementation: format findings and post to GitHub."""
    import httpx

    settings = get_settings()
    job_id = payload.get("job_id", "unknown")
    repo_full_name = payload.get("repo_full_name", "")
    pr_number = payload.get("pr_number", 0)
    head_sha = payload.get("head_sha", "")
    merged_findings = payload.get("merged_findings", [])
    agent_results = payload.get("agent_results", [])

    log = logger.bind(
        job_id=job_id,
        repo=repo_full_name,
        pr_number=pr_number,
        findings=len(merged_findings),
    )

    log.info("Posting review comment to GitHub")

    # ── Format the review body ─────────────────────────────────────────────
    body = _format_review_body(merged_findings, agent_results)

    # ── Determine overall review event ────────────────────────────────────
    # Always use COMMENT — GitHub forbids REQUEST_CHANGES if the reviewer
    # is the same user who opened the PR (which is the case when using a PAT).
    # In production with a GitHub App bot account, this restriction doesn't apply.
    event = "COMMENT"

    # ── Submit the review via GitHub API ───────────────────────────────────
    pat = settings.github_pat
    if not pat:
        log.error("No GitHub PAT configured — cannot post review")
        return {"status": "error", "reason": "no_github_pat"}

    url = f"https://api.github.com/repos/{repo_full_name}/pulls/{pr_number}/reviews"
    headers = {
        "Authorization": f"Bearer {pat}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "pr-reviewer-bot/0.1.0",
    }
    review_payload = {
        "commit_id": head_sha,
        "body": body,
        "event": event,
    }

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(url, headers=headers, json=review_payload)
        if response.status_code not in (200, 201):
            log.error(
                "Failed to post review to GitHub",
                status_code=response.status_code,
                response=response.text[:500],
            )
            raise RuntimeError(
                f"GitHub API returned {response.status_code}: {response.text[:200]}"
            )

    log.info(
        "Successfully posted review to GitHub",
        review_event=event,
        findings=len(merged_findings),
    )
    return {
        "status": "posted",
        "event": event,
        "findings_count": len(merged_findings),
        "pr_number": pr_number,
        "repo": repo_full_name,
    }


def _format_review_body(merged_findings: list[dict], agent_results: list[dict]) -> str:
    """Format the AI findings into a clean, readable GitHub review comment."""
    from collections import defaultdict

    SEVERITY_EMOJI = {
        "critical": "🔴",
        "high": "🟠",
        "medium": "🟡",
        "low": "🔵",
        "info": "⚪",
    }

    AGENT_EMOJI = {
        "security": "🔒",
        "architecture": "🏗️",
        "static_analysis": "🔍",
        "style": "✨",
    }

    lines = ["## 🤖 AI Code Review\n"]

    # ── Summary table ──────────────────────────────────────────────────────
    if not merged_findings:
        lines.append("✅ **No issues found!** The code looks good.\n")
    else:
        # Count by severity
        severity_counts: dict[str, int] = defaultdict(int)
        for f in merged_findings:
            severity_counts[f.get("severity", "info")] += 1

        summary_parts = []
        for sev in ("critical", "high", "medium", "low", "info"):
            count = severity_counts.get(sev, 0)
            if count:
                summary_parts.append(f"{SEVERITY_EMOJI[sev]} **{count} {sev}**")

        lines.append(f"Found **{len(merged_findings)} issue(s)**: {' · '.join(summary_parts)}\n")

    # ── Agent summaries ─────────────────────────────────────────────────────
    if agent_results:
        lines.append("### Agent Summaries\n")
        for ar in agent_results:
            agent_type = ar.get("agent_type", "unknown")
            summary = ar.get("summary", "")
            emoji = AGENT_EMOJI.get(agent_type, "🤖")
            if summary and not summary.startswith("Agent failed"):
                lines.append(f"- {emoji} **{agent_type.replace('_', ' ').title()}**: {summary}")
        lines.append("")

    # ── Findings grouped by severity ────────────────────────────────────────
    if merged_findings:
        lines.append("---\n")
        lines.append("### Findings\n")

        # Group by severity
        by_severity: dict[str, list[dict]] = defaultdict(list)
        for f in merged_findings:
            by_severity[f.get("severity", "info")].append(f)

        for sev in ("critical", "high", "medium", "low", "info"):
            findings_at_sev = by_severity.get(sev, [])
            if not findings_at_sev:
                continue

            emoji = SEVERITY_EMOJI[sev]
            lines.append(f"#### {emoji} {sev.upper()}\n")

            for finding in findings_at_sev:
                title = finding.get("title", "Untitled Finding")
                description = finding.get("description", "")
                suggestion = finding.get("suggestion")
                location = finding.get("location")
                agent_type = finding.get("agent_type", "")

                agent_emoji = AGENT_EMOJI.get(agent_type, "🤖")
                lines.append(f"**{title}** {agent_emoji}")

                if location:
                    file_path = location.get("file_path", "")
                    line_start = location.get("line_start", "")
                    if file_path:
                        lines.append(f"> 📄 `{file_path}` (line {line_start})")

                if description:
                    lines.append(f"\n{description}")

                if suggestion:
                    lines.append(f"\n💡 **Suggestion:**\n{suggestion}")

                lines.append("\n---")

    # ── Footer ──────────────────────────────────────────────────────────────
    lines.append(
        "\n<sub>Generated by [ReviewForge AI](https://github.com/nikhilesh-git/github_pr_code_reviewer) "
        "· Powered by OpenRouter</sub>"
    )

    return "\n".join(lines)


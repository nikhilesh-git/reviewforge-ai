"""SQLAlchemy 2.0 ORM models — the persistence layer.

Naming conventions used throughout:
- Table names: snake_case, plural (e.g. ``pr_events``, ``review_jobs``)
- Column names: snake_case
- Relationships: lazy="selectin" for async compatibility (no lazy-load)
- All timestamps: timezone-aware (TIMESTAMPTZ in PostgreSQL)
- UUIDs: stored as native PostgreSQL UUID type via ``UUID(as_uuid=True)``
- JSON: stored as JSONB for indexing and querying capabilities

All ORM models inherit from ``Base`` in ``shared.infrastructure.database``.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from shared.infrastructure.database import Base


class PREventRecord(Base):
    """Persisted record of a GitHub webhook event.

    Created immediately when the gateway receives a webhook, before any
    validation or processing. Used for auditing, deduplication, and replay.

    The ``unique_key`` column enforces idempotency — if GitHub delivers the
    same event twice (which it occasionally does), the second insert fails
    with a unique constraint violation and is silently ignored.
    """

    __tablename__ = "pr_events"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        comment="Internal UUID primary key",
    )
    delivery_id: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        unique=True,
        index=True,
        comment="GitHub X-GitHub-Delivery header — globally unique per delivery",
    )
    event_type: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        comment="GitHub X-GitHub-Event header (e.g. pull_request)",
    )
    action: Mapped[str | None] = mapped_column(
        String(50),
        nullable=True,
        comment="PR action: opened, synchronize, reopened, closed",
    )
    repository_full_name: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        index=True,
        comment="owner/repo format",
    )
    pr_number: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        index=True,
    )
    head_sha: Mapped[str | None] = mapped_column(
        String(40),
        nullable=True,
        comment="Full 40-char SHA of the PR head commit",
    )
    base_sha: Mapped[str | None] = mapped_column(
        String(40),
        nullable=True,
    )
    author_login: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
    )
    installation_id: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="GitHub App installation ID",
    )
    processing_status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="received",
        index=True,
        comment="EventProcessingStatus enum value",
    )
    unique_key: Mapped[str] = mapped_column(
        String(500),
        nullable=False,
        unique=True,
        comment="Deduplication key: repo#pr_number@sha:action",
    )
    raw_payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        comment="Full raw GitHub webhook payload for auditing",
    )
    error_message: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Error details if processing_status = failed",
    )
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        comment="Timestamp when the webhook was received by the gateway",
    )
    processed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="Timestamp when the event was successfully enqueued",
    )

    # Relationship to review job (one event → one job)
    review_job: Mapped["ReviewJobRecord | None"] = relationship(
        "ReviewJobRecord",
        back_populates="pr_event",
        lazy="selectin",
    )

    __table_args__ = (
        Index("ix_pr_events_repo_pr", "repository_full_name", "pr_number"),
        Index("ix_pr_events_status_received", "processing_status", "received_at"),
    )

    def __repr__(self) -> str:
        return (
            f"<PREventRecord id={self.id} "
            f"repo={self.repository_full_name} "
            f"pr=#{self.pr_number} "
            f"status={self.processing_status}>"
        )


class ReviewJobRecord(Base):
    """Tracks the lifecycle of a PR review job.

    One ReviewJob is created per PR event that passes validation.
    Its status field tracks progress through the LangGraph pipeline.

    The ``findings`` JSONB column stores the merged, de-duplicated findings
    from all four agents — serialized as a list of ReviewFinding dicts.
    Keeping findings in JSONB (vs. a separate table) makes reads fast and
    avoids complex JOINs for the most common query pattern.
    """

    __tablename__ = "review_jobs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        comment="Stable review job ID — used in Celery task args",
    )
    pr_event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("pr_events.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    celery_task_id: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        index=True,
        comment="Celery AsyncResult task ID for status polling",
    )
    status: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        default="pending",
        index=True,
        comment="JobStatus enum value",
    )
    priority: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=5,
        comment="Job priority 1–10 (higher = processed first)",
    )

    # Agent results — one JSONB entry per agent
    agent_results: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        comment="List of AgentResult dicts, one per agent",
    )

    # Merged findings (deduplication output)
    findings: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        comment="Merged, de-duplicated list of ReviewFinding dicts",
    )

    # Statistics
    total_findings: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    critical_findings: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tokens_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # GitHub review details
    github_review_id: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="GitHub Pull Request Review ID after posting",
    )
    github_comments_posted: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
    )

    # Error tracking
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    # Relationship
    pr_event: Mapped["PREventRecord"] = relationship(
        "PREventRecord",
        back_populates="review_job",
        lazy="selectin",
    )

    __table_args__ = (
        Index("ix_review_jobs_status_created", "status", "created_at"),
        Index("ix_review_jobs_priority_created", "priority", "created_at"),
    )

    def __repr__(self) -> str:
        return (
            f"<ReviewJobRecord id={self.id} "
            f"status={self.status} "
            f"findings={self.total_findings}>"
        )

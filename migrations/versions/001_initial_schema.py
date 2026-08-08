"""Initial database migration — create pr_events and review_jobs tables.

Revision ID: 001_initial
Revises: None
Create Date: 2026-08-07 00:00:00.000000 UTC

This is the baseline migration. Running ``alembic upgrade head`` from a
fresh database will apply this migration and create all required tables.

Tables created:
- pr_events: Persists incoming GitHub webhook events
- review_jobs: Tracks the lifecycle of PR review jobs
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

# revision identifiers, used by Alembic.
revision: str = "001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create initial schema: pr_events and review_jobs tables."""

    # ── pr_events ─────────────────────────────────────────────────────────────
    op.create_table(
        "pr_events",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
            comment="Internal UUID primary key",
        ),
        sa.Column(
            "delivery_id",
            sa.String(64),
            nullable=False,
            comment="GitHub X-GitHub-Delivery header — globally unique per delivery",
        ),
        sa.Column(
            "event_type",
            sa.String(50),
            nullable=False,
            comment="GitHub X-GitHub-Event header",
        ),
        sa.Column(
            "action",
            sa.String(50),
            nullable=True,
            comment="PR action: opened, synchronize, reopened, closed",
        ),
        sa.Column(
            "repository_full_name",
            sa.String(255),
            nullable=True,
            comment="owner/repo format",
        ),
        sa.Column("pr_number", sa.Integer(), nullable=True),
        sa.Column(
            "head_sha",
            sa.String(40),
            nullable=True,
            comment="Full 40-char SHA of the PR head commit",
        ),
        sa.Column("base_sha", sa.String(40), nullable=True),
        sa.Column("author_login", sa.String(100), nullable=True),
        sa.Column(
            "installation_id",
            sa.Integer(),
            nullable=True,
            comment="GitHub App installation ID",
        ),
        sa.Column(
            "processing_status",
            sa.String(20),
            nullable=False,
            default="received",
            comment="EventProcessingStatus enum value",
        ),
        sa.Column(
            "unique_key",
            sa.String(500),
            nullable=False,
            comment="Deduplication key: repo#pr_number@sha:action",
        ),
        sa.Column(
            "raw_payload",
            JSONB(),
            nullable=False,
            comment="Full raw GitHub webhook payload for auditing",
        ),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
    )

    # Indexes for pr_events
    op.create_index("ix_pr_events_delivery_id", "pr_events", ["delivery_id"], unique=True)
    op.create_index("ix_pr_events_unique_key", "pr_events", ["unique_key"], unique=True)
    op.create_index("ix_pr_events_repo_fullname", "pr_events", ["repository_full_name"])
    op.create_index("ix_pr_events_pr_number", "pr_events", ["pr_number"])
    op.create_index("ix_pr_events_processing_status", "pr_events", ["processing_status"])
    op.create_index(
        "ix_pr_events_repo_pr",
        "pr_events",
        ["repository_full_name", "pr_number"],
    )
    op.create_index(
        "ix_pr_events_status_received",
        "pr_events",
        ["processing_status", "received_at"],
    )

    # ── review_jobs ───────────────────────────────────────────────────────────
    op.create_table(
        "review_jobs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "pr_event_id",
            UUID(as_uuid=True),
            sa.ForeignKey("pr_events.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("celery_task_id", sa.String(255), nullable=True),
        sa.Column("status", sa.String(30), nullable=False, default="pending"),
        sa.Column("priority", sa.Integer(), nullable=False, default=5),
        sa.Column("agent_results", JSONB(), nullable=False, default=list),
        sa.Column("findings", JSONB(), nullable=False, default=list),
        sa.Column("total_findings", sa.Integer(), nullable=False, default=0),
        sa.Column("critical_findings", sa.Integer(), nullable=False, default=0),
        sa.Column("tokens_used", sa.Integer(), nullable=False, default=0),
        sa.Column("github_review_id", sa.Integer(), nullable=True),
        sa.Column("github_comments_posted", sa.Integer(), nullable=False, default=0),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("retry_count", sa.Integer(), nullable=False, default=0),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )

    # Indexes for review_jobs
    op.create_index("ix_review_jobs_pr_event_id", "review_jobs", ["pr_event_id"], unique=True)
    op.create_index("ix_review_jobs_celery_task_id", "review_jobs", ["celery_task_id"])
    op.create_index("ix_review_jobs_status", "review_jobs", ["status"])
    op.create_index(
        "ix_review_jobs_status_created",
        "review_jobs",
        ["status", "created_at"],
    )
    op.create_index(
        "ix_review_jobs_priority_created",
        "review_jobs",
        ["priority", "created_at"],
    )


def downgrade() -> None:
    """Drop all tables created in this migration."""
    # Drop tables in reverse dependency order
    op.drop_table("review_jobs")
    op.drop_table("pr_events")

"""Core Pydantic v2 domain models for the GitHub PR Code Reviewer platform.

These are pure domain objects — no SQLAlchemy, no HTTP framework concerns.
They represent the canonical shape of data flowing through the system.

Design decisions:
- All IDs are UUID v4 for global uniqueness without DB coordination.
- All datetimes are timezone-aware (UTC).
- ``model_config = ConfigDict(frozen=True)`` on value objects prevents accidental mutation.
- Discriminated unions are used where the type of object determines its shape.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

UTC = timezone.utc  # datetime.UTC was added in Python 3.11; this is the 3.10-compat alias
from typing import Any

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, field_validator, model_validator

from shared.domain.enums import (
    AgentType,
    EventProcessingStatus,
    JobStatus,
    OWASPCategory,
    PRAction,
    Severity,
)


# ─── Value Objects (immutable) ───────────────────────────────────────────────


class RepositoryRef(BaseModel):
    """Identifies a GitHub repository."""

    model_config = ConfigDict(frozen=True)

    owner: str = Field(..., min_length=1, max_length=100, description="GitHub org or user")
    name: str = Field(..., min_length=1, max_length=100, description="Repository name")

    @property
    def full_name(self) -> str:
        """Canonical owner/repo format used throughout GitHub APIs."""
        return f"{self.owner}/{self.name}"

    @classmethod
    def from_full_name(cls, full_name: str) -> "RepositoryRef":
        """Parse 'owner/repo' string into a RepositoryRef."""
        parts = full_name.split("/", 1)
        if len(parts) != 2:  # noqa: PLR2004
            msg = f"Invalid repository full_name: {full_name!r}. Expected 'owner/repo'."
            raise ValueError(msg)
        return cls(owner=parts[0], name=parts[1])

    def __str__(self) -> str:
        return self.full_name


class CommitRef(BaseModel):
    """Identifies a specific commit."""

    model_config = ConfigDict(frozen=True)

    sha: str = Field(..., min_length=40, max_length=40, description="Full 40-char SHA")
    ref: str | None = Field(None, description="Branch or tag ref (e.g. refs/heads/main)")

    @field_validator("sha")
    @classmethod
    def sha_must_be_hex(cls, v: str) -> str:
        """Validate that the SHA is a valid hex string."""
        if not all(c in "0123456789abcdefABCDEF" for c in v):
            msg = f"SHA must be hexadecimal, got: {v!r}"
            raise ValueError(msg)
        return v.lower()


# ─── GitHub PR Event ─────────────────────────────────────────────────────────


class PRAuthor(BaseModel):
    """The GitHub user who opened the pull request."""

    model_config = ConfigDict(frozen=True)

    login: str = Field(..., description="GitHub username")
    avatar_url: AnyHttpUrl | None = Field(None)
    type: str = Field(default="User", description="User or Bot")


class PREvent(BaseModel):
    """Canonical representation of an incoming GitHub Pull Request webhook event.

    This is the domain model created from the raw GitHub webhook payload.
    It carries everything needed to start a review job.
    """

    model_config = ConfigDict(frozen=True)

    # Identity
    delivery_id: str = Field(..., description="GitHub X-GitHub-Delivery header value")
    action: PRAction = Field(..., description="PR lifecycle event action")

    # Repository
    repository: RepositoryRef

    # Pull Request metadata
    pr_number: int = Field(..., gt=0)
    pr_title: str = Field(..., min_length=1, max_length=500)
    pr_body: str | None = Field(None, description="PR description (may be None)")
    pr_url: AnyHttpUrl = Field(..., description="GitHub HTML URL of the PR")

    # Commits
    head: CommitRef = Field(..., description="The branch being merged in")
    base: CommitRef = Field(..., description="The target branch")

    # Author
    author: PRAuthor

    # GitHub App integration
    installation_id: int | None = Field(
        None, description="GitHub App installation ID (used to get access token)"
    )

    # Timing
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def unique_key(self) -> str:
        """Deduplication key — same PR + same commit should not be reviewed twice."""
        return f"{self.repository.full_name}#{self.pr_number}@{self.head.sha}:{self.action}"

    @property
    def diff_url(self) -> str:
        """GitHub diff API endpoint for this PR."""
        return (
            f"https://api.github.com/repos/{self.repository.full_name}"
            f"/pulls/{self.pr_number}"
        )

    @property
    def files_url(self) -> str:
        """GitHub files API endpoint for this PR."""
        return (
            f"https://api.github.com/repos/{self.repository.full_name}"
            f"/pulls/{self.pr_number}/files"
        )


# ─── Review Findings ─────────────────────────────────────────────────────────


class CodeLocation(BaseModel):
    """Precise location of a finding within a file."""

    model_config = ConfigDict(frozen=True)

    file_path: str = Field(..., min_length=1, description="Relative path within the repository")
    line_start: int = Field(..., gt=0, description="1-indexed start line")
    line_end: int | None = Field(None, description="1-indexed end line (None = single line)")
    side: str = Field(default="RIGHT", pattern="^(LEFT|RIGHT)$", description="Diff side")

    @model_validator(mode="after")
    def validate_line_range(self) -> "CodeLocation":
        """Ensure line_end >= line_start when provided."""
        if self.line_end is not None and self.line_end < self.line_start:
            msg = f"line_end ({self.line_end}) must be >= line_start ({self.line_start})"
            raise ValueError(msg)
        return self


class ReviewFinding(BaseModel):
    """A single finding produced by an AI review agent.

    Findings are the core output of the platform — they become GitHub
    review comments, and are stored in PostgreSQL for deduplication and
    learning purposes.
    """

    model_config = ConfigDict(frozen=True)

    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    agent_type: AgentType
    severity: Severity
    location: CodeLocation | None = Field(
        None,
        description="Code location (None for PR-level findings without a specific line)",
    )
    title: str = Field(..., min_length=1, max_length=200, description="Short finding title")
    description: str = Field(
        ..., min_length=10, description="Detailed explanation of the issue"
    )
    suggestion: str | None = Field(
        None, description="Concrete code fix or improvement suggestion"
    )
    # Security-specific
    owasp_category: OWASPCategory | None = Field(
        None, description="OWASP Top 10 category (security findings only)"
    )
    cwe_id: str | None = Field(
        None,
        pattern=r"^CWE-\d+$",
        description="CWE identifier (e.g. CWE-89 for SQL Injection)",
    )
    # Metadata
    confidence: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        description="Agent confidence in this finding (0.0–1.0)",
    )
    tags: list[str] = Field(default_factory=list, description="Freeform categorization tags")

    @property
    def fingerprint(self) -> str:
        """Content-based fingerprint for deduplication.

        Two findings with the same fingerprint describe the same issue
        at the same location, regardless of which agent produced them.
        """
        loc_str = ""
        if self.location:
            loc_str = f"{self.location.file_path}:{self.location.line_start}"
        return f"{self.agent_type}:{self.severity}:{loc_str}:{self.title[:50]}"


class AgentResult(BaseModel):
    """Complete output from a single AI agent's review pass."""

    agent_type: AgentType
    findings: list[ReviewFinding] = Field(default_factory=list)
    summary: str = Field(..., description="Agent's overall summary of this review area")
    tokens_used: int = Field(default=0, ge=0)
    latency_ms: float = Field(default=0.0, ge=0.0, description="Wall-clock agent latency")
    model_used: str = Field(..., description="LLM model identifier that produced this result")
    error: str | None = Field(
        None, description="Error message if agent failed (findings may be partial)"
    )

    @property
    def has_blocking_findings(self) -> bool:
        """True if any finding has a severity that should block merge."""
        return any(f.severity in Severity.blocking() for f in self.findings)

    @property
    def findings_by_severity(self) -> dict[Severity, list[ReviewFinding]]:
        """Group findings by severity level for reporting."""
        result: dict[Severity, list[ReviewFinding]] = {}
        for finding in self.findings:
            result.setdefault(finding.severity, []).append(finding)
        return result


# ─── Review Job ──────────────────────────────────────────────────────────────


class ReviewJobCreate(BaseModel):
    """Input model for creating a new review job."""

    pr_event: PREvent
    priority: int = Field(default=5, ge=1, le=10, description="Job priority (1=low, 10=high)")


class ReviewJob(BaseModel):
    """Complete review job domain model — includes results from all agents."""

    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    pr_event: PREvent
    status: JobStatus = Field(default=JobStatus.PENDING)
    priority: int = Field(default=5)

    # Results (populated as agents complete)
    agent_results: list[AgentResult] = Field(default_factory=list)
    merged_findings: list[ReviewFinding] = Field(default_factory=list)

    # Timing
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    started_at: datetime | None = None
    completed_at: datetime | None = None

    # Celery task tracking
    celery_task_id: str | None = None

    @property
    def duration_seconds(self) -> float | None:
        """Total wall-clock time from creation to completion."""
        if self.started_at and self.completed_at:
            return (self.completed_at - self.started_at).total_seconds()
        return None

    @property
    def total_findings(self) -> int:
        """Count of de-duplicated findings across all agents."""
        return len(self.merged_findings)

    @property
    def critical_finding_count(self) -> int:
        """Count of critical + high severity findings."""
        return sum(
            1
            for f in self.merged_findings
            if f.severity in (Severity.CRITICAL, Severity.HIGH)
        )


# ─── Repository Convention (Learner Service) ─────────────────────────────────


class RepositoryConvention(BaseModel):
    """A coding convention extracted by the Learner service from merged PRs.

    Stored in Qdrant as a vector embedding to provide contextual guidance
    to AI agents when reviewing future PRs in the same repository.
    """

    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    repository: RepositoryRef
    convention_type: str = Field(
        ...,
        description="Category: naming, testing, error_handling, logging, etc.",
    )
    description: str = Field(
        ...,
        min_length=20,
        description="Human-readable description of the convention",
    )
    example_code: str | None = Field(None, description="Code snippet illustrating the convention")
    source_pr_number: int | None = Field(None, description="PR from which this was extracted")
    occurrence_count: int = Field(
        default=1, ge=1, description="How many times this pattern has been observed"
    )
    confidence_score: float = Field(default=0.7, ge=0.0, le=1.0)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    last_seen_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    # Qdrant metadata (not a vector — stored in payload)
    embedding_text: str | None = Field(
        None,
        description="Text that was embedded into the vector (generated from description + example)",
        exclude=True,  # Do not include in standard serialization
    )


# ─── Webhook Event Record ─────────────────────────────────────────────────────


class WebhookEventRecord(BaseModel):
    """Immutable record of a received GitHub webhook event.

    Created immediately on receipt, before any processing.
    Used for auditing, deduplication, and replay.
    """

    model_config = ConfigDict(frozen=True)

    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    delivery_id: str = Field(..., description="GitHub X-GitHub-Delivery header")
    event_type: str = Field(..., description="GitHub X-GitHub-Event header")
    action: str | None = Field(None)
    repository_full_name: str | None = Field(None)
    pr_number: int | None = Field(None)
    head_sha: str | None = Field(None)
    processing_status: EventProcessingStatus = Field(default=EventProcessingStatus.RECEIVED)
    raw_payload: dict[str, Any] = Field(default_factory=dict)
    received_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    processed_at: datetime | None = None
    error_message: str | None = None

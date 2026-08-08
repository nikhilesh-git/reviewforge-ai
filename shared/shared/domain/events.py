"""Redis stream event schemas for inter-service communication.

Events published to Redis Streams are serialized to JSON. These Pydantic
models define the wire format — they are NOT the same as domain models,
which may have fields not suitable for the queue (e.g. raw payloads).

Stream architecture:
  - Producer: gateway
  - Stream key: pr:events
  - Consumer group: review-workers
  - Consumers: worker service (one per Celery worker process)
"""

from __future__ import annotations

from datetime import datetime, timezone

UTC = timezone.utc
from typing import Literal
import uuid

from pydantic import BaseModel, ConfigDict, Field

from shared.domain.enums import PRAction


class StreamMessage(BaseModel):
    """Base class for all Redis Stream messages."""

    model_config = ConfigDict(frozen=True)

    message_id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        description="Unique message identifier (distinct from Redis stream entry ID)",
    )
    schema_version: str = Field(
        default="1.0",
        description="Schema version for forward-compatibility checks",
    )
    produced_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class PRReviewRequestedEvent(StreamMessage):
    """Event published to Redis stream when a PR is ready for review.

    This is the primary event that triggers the full multi-agent review pipeline.
    The worker service consumes this and starts a LangGraph run.
    """

    event_type: Literal["pr_review_requested"] = "pr_review_requested"

    # Event identity
    delivery_id: str = Field(..., description="GitHub delivery ID (for idempotency)")
    job_id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        description="Unique review job ID (pre-assigned by gateway)",
    )

    # PR metadata — enough to fetch the diff without a DB lookup
    repo_full_name: str = Field(..., description="owner/repo")
    pr_number: int = Field(..., gt=0)
    pr_title: str
    action: PRAction
    head_sha: str = Field(..., min_length=40, max_length=40)
    base_sha: str = Field(..., min_length=40, max_length=40)
    pr_url: str
    author_login: str

    # GitHub App
    installation_id: int | None = None

    # Job configuration
    priority: int = Field(default=5, ge=1, le=10)

    def to_redis_dict(self) -> dict[str, str]:
        """Serialize to a flat dict of strings for Redis XADD.

        Redis Streams store all values as byte strings — nested JSON
        is serialized as a single "data" field to keep it simple.
        """
        import json
        return {"data": self.model_dump_json(), "event_type": self.event_type}

    @classmethod
    def from_redis_dict(cls, raw: dict[bytes | str, bytes | str]) -> "PRReviewRequestedEvent":
        """Deserialize from Redis XREAD output."""
        import json

        # Redis returns bytes keys/values unless decode_responses=True
        decoded = {
            (k.decode() if isinstance(k, bytes) else k): (
                v.decode() if isinstance(v, bytes) else v
            )
            for k, v in raw.items()
        }
        return cls.model_validate_json(decoded["data"])


class PRLearnRequestedEvent(StreamMessage):
    """Event published when a PR is merged — triggers the Learner service."""

    event_type: Literal["pr_learn_requested"] = "pr_learn_requested"

    repo_full_name: str
    pr_number: int = Field(..., gt=0)
    head_sha: str
    merged_at: datetime
    installation_id: int | None = None

    def to_redis_dict(self) -> dict[str, str]:
        """Serialize to a flat dict of strings for Redis XADD."""
        return {"data": self.model_dump_json(), "event_type": self.event_type}

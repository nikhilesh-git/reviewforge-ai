"""Event repository — persistence layer for incoming webhook events.

Implements the Repository Pattern: the domain/use-case layer calls methods on
this class without knowing anything about SQLAlchemy, PostgreSQL, or SQL.

Design decisions:
- All methods are async to match the async request lifecycle.
- Methods return domain models (Pydantic), not ORM models. The conversion
  happens inside the repository — callers never deal with ORM objects.
- ``save`` uses INSERT ... ON CONFLICT DO UPDATE to be idempotent — the same
  delivery can be inserted twice safely (GitHub occasionally retries).
- The repository raises ``DuplicateEventError`` for events that were already
  processed (different from the INSERT idempotency — a duplicate is when we
  have already QUEUED this event).
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

UTC = timezone.utc

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from shared.domain.enums import EventProcessingStatus
from shared.domain.models import WebhookEventRecord
from shared.infrastructure.orm_models import PREventRecord

logger = logging.getLogger(__name__)


class DuplicateEventError(Exception):
    """Raised when a webhook event has already been processed.

    This is a domain error — the caller should return 200 OK (not an error)
    because GitHub expects a 2xx response even for duplicates.
    """

    def __init__(self, delivery_id: str, unique_key: str) -> None:
        self.delivery_id = delivery_id
        self.unique_key = unique_key
        super().__init__(
            f"Duplicate event: delivery_id={delivery_id!r}, key={unique_key!r}"
        )


class EventRepository:
    """Repository for persisting and querying GitHub webhook event records.

    Args:
        session: An active ``AsyncSession`` (injected via DI).
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def save_received(
        self,
        *,
        delivery_id: str,
        event_type: str,
        action: str | None,
        repository_full_name: str | None,
        pr_number: int | None,
        head_sha: str | None,
        base_sha: str | None,
        author_login: str | None,
        installation_id: int | None,
        unique_key: str,
        raw_payload: dict,
    ) -> PREventRecord:
        """Persist a newly received webhook event with status 'received'.

        Uses PostgreSQL's ``INSERT ... ON CONFLICT (delivery_id) DO NOTHING``
        to safely handle GitHub's occasional duplicate deliveries.

        Args:
            delivery_id: GitHub X-GitHub-Delivery header value.
            event_type: GitHub X-GitHub-Event header value.
            ...

        Returns:
            The persisted ``PREventRecord`` ORM model.

        Raises:
            DuplicateEventError: If this ``delivery_id`` was already received
                and is in a terminal processing state (queued, ignored).
        """
        record_id = uuid.uuid4()
        now = datetime.now(UTC)

        # Use upsert to handle duplicate delivery IDs gracefully
        stmt = (
            pg_insert(PREventRecord)
            .values(
                id=record_id,
                delivery_id=delivery_id,
                event_type=event_type,
                action=action,
                repository_full_name=repository_full_name,
                pr_number=pr_number,
                head_sha=head_sha,
                base_sha=base_sha,
                author_login=author_login,
                installation_id=installation_id,
                processing_status=EventProcessingStatus.RECEIVED,
                unique_key=unique_key,
                raw_payload=raw_payload,
                received_at=now,
            )
            .on_conflict_do_nothing(index_elements=["delivery_id"])
            .returning(PREventRecord)
        )

        result = await self._session.execute(stmt)
        record = result.scalar_one_or_none()

        if record is None:
            # INSERT was a no-op — delivery_id already exists
            # Fetch the existing record to check its status
            existing = await self.get_by_delivery_id(delivery_id)
            if existing and existing.processing_status in (
                EventProcessingStatus.QUEUED,
                EventProcessingStatus.IGNORED,
            ):
                raise DuplicateEventError(delivery_id, unique_key)
            # If status is 'received' or 'failed', allow re-processing
            return existing  # type: ignore[return-value]

        logger.debug(
            "Saved webhook event record",
            extra={
                "event_id": str(record_id),
                "delivery_id": delivery_id,
                "event_type": event_type,
            },
        )
        return record

    async def mark_queued(
        self,
        delivery_id: str,
        *,
        stream_entry_id: str,
    ) -> None:
        """Update the event status to 'queued' after successful stream publish.

        Args:
            delivery_id: GitHub delivery ID.
            stream_entry_id: Redis stream entry ID (for auditing).
        """
        now = datetime.now(UTC)
        stmt = (
            update(PREventRecord)
            .where(PREventRecord.delivery_id == delivery_id)
            .values(
                processing_status=EventProcessingStatus.QUEUED,
                processed_at=now,
                # Store the stream entry ID in raw_payload for debugging
            )
        )
        await self._session.execute(stmt)
        logger.debug(
            "Marked event as queued",
            extra={"delivery_id": delivery_id, "stream_entry_id": stream_entry_id},
        )

    async def mark_ignored(
        self,
        delivery_id: str,
        reason: str,
    ) -> None:
        """Mark an event as deliberately ignored (e.g. non-reviewable action).

        Args:
            delivery_id: GitHub delivery ID.
            reason: Human-readable reason for ignoring.
        """
        stmt = (
            update(PREventRecord)
            .where(PREventRecord.delivery_id == delivery_id)
            .values(
                processing_status=EventProcessingStatus.IGNORED,
                processed_at=datetime.now(UTC),
                error_message=reason,
            )
        )
        await self._session.execute(stmt)

    async def mark_failed(
        self,
        delivery_id: str,
        error_message: str,
    ) -> None:
        """Mark an event as failed.

        Args:
            delivery_id: GitHub delivery ID.
            error_message: Error details for debugging.
        """
        stmt = (
            update(PREventRecord)
            .where(PREventRecord.delivery_id == delivery_id)
            .values(
                processing_status=EventProcessingStatus.FAILED,
                processed_at=datetime.now(UTC),
                error_message=error_message[:2000],  # Truncate for DB storage
            )
        )
        await self._session.execute(stmt)

    async def get_by_delivery_id(self, delivery_id: str) -> PREventRecord | None:
        """Fetch a webhook event record by its GitHub delivery ID.

        Args:
            delivery_id: GitHub X-GitHub-Delivery header value.

        Returns:
            The ``PREventRecord`` if found, or None.
        """
        stmt = select(PREventRecord).where(PREventRecord.delivery_id == delivery_id)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def exists_by_unique_key(self, unique_key: str) -> bool:
        """Check if an event with this unique key has already been queued.

        The unique key encodes repo + pr_number + head_sha + action, so
        the same commit triggering the same event twice is detected here.

        Args:
            unique_key: Deduplication key string.

        Returns:
            True if a queued event with this key already exists.
        """
        stmt = select(PREventRecord.id).where(
            PREventRecord.unique_key == unique_key,
            PREventRecord.processing_status == EventProcessingStatus.QUEUED,
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none() is not None

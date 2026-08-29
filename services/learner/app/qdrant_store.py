"""Qdrant vector store operations for the learner service.

Handles embedding text and upserting repository conventions into the
Qdrant collection that backs the Style agent's contextual memory.
"""

from __future__ import annotations

import logging
import uuid

from qdrant_client import AsyncQdrantClient
from qdrant_client.http.models import (
    Distance,
    FieldCondition,
    Filter,
    MatchValue,
    PointStruct,
    VectorParams,
)

from shared.domain.models import RepositoryConvention

logger = logging.getLogger(__name__)


class ConventionVectorStore:
    """Manages vector storage and retrieval for repository conventions.

    Args:
        host: Qdrant server hostname.
        port: Qdrant REST API port.
        collection_name: Target collection name.
        embedding_dim: Embedding vector dimensionality.
    """

    def __init__(
        self,
        host: str = "localhost",
        port: int = 6333,
        collection_name: str = "repo_conventions",
        embedding_dim: int = 1536,
        url: str | None = None,
        api_key: str | None = None,
    ) -> None:
        if url:
            self._client = AsyncQdrantClient(url=url, api_key=api_key)
        else:
            self._client = AsyncQdrantClient(host=host, port=port)
        self._collection_name = collection_name
        self._embedding_dim = embedding_dim

    async def ensure_collection(self) -> None:
        """Create the collection if it doesn't exist."""
        try:
            await self._client.get_collection(self._collection_name)
        except Exception:
            await self._client.create_collection(
                collection_name=self._collection_name,
                vectors_config=VectorParams(
                    size=self._embedding_dim,
                    distance=Distance.COSINE,
                ),
            )
            logger.info(
                "Created Qdrant collection",
                extra={"collection": self._collection_name},
            )

    async def upsert_convention(
        self,
        convention: RepositoryConvention,
        vector: list[float],
    ) -> None:
        """Upsert a convention into the vector store.

        Uses the convention's UUID as the point ID for idempotent upserts —
        if the same convention is seen again (same PR re-processed), it simply
        updates the existing entry rather than creating a duplicate.

        Args:
            convention: The domain convention object to store.
            vector: Pre-computed embedding vector for the convention text.
        """
        from shared.infrastructure.metrics import LEARNER_CONVENTIONS_STORED_TOTAL

        # Convert UUID to integer for Qdrant (it supports both str and int IDs)
        point_id = str(convention.id)

        point = PointStruct(
            id=point_id,
            vector=vector,
            payload={
                "repo_full_name": convention.repository.full_name,
                "description": convention.description,
                "convention_type": convention.convention_type,
                "example_code": convention.example_code,
                "confidence_score": convention.confidence_score,
                "source_pr_number": convention.source_pr_number,
                "occurrence_count": convention.occurrence_count,
                "created_at": convention.created_at.isoformat(),
                "last_seen_at": convention.last_seen_at.isoformat(),
            },
        )

        await self._client.upsert(
            collection_name=self._collection_name,
            points=[point],
        )

        LEARNER_CONVENTIONS_STORED_TOTAL.labels(status="success").inc()
        logger.info(
            "Convention upserted to Qdrant",
            extra={
                "id": point_id,
                "repo": convention.repository.full_name,
                "type": convention.convention_type,
            },
        )

    async def get_repo_convention_count(self, repo_full_name: str) -> int:
        """Get the number of conventions stored for a repository.

        Args:
            repo_full_name: Repository in ``owner/repo`` format.

        Returns:
            Count of conventions in the vector store for this repo.
        """
        repo_filter = Filter(
            must=[
                FieldCondition(
                    key="repo_full_name",
                    match=MatchValue(value=repo_full_name),
                )
            ]
        )
        result = await self._client.count(
            collection_name=self._collection_name,
            count_filter=repo_filter,
        )
        return result.count

    async def close(self) -> None:
        """Close the Qdrant client."""
        await self._client.close()

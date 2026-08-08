"""Qdrant vector store client for the worker service.

Used to retrieve repository-specific coding conventions that were previously
learned from merged PRs. These conventions are injected into the Style agent's
context to make reviews repo-aware.

Design decisions:
- Uses ``qdrant_client`` with async support for non-blocking I/O.
- Conventions are stored with a ``repo_full_name`` payload filter so each
  repository has isolated memory.
- Similarity search uses cosine distance on text embeddings.
- Embeddings are generated via the same OpenRouter-compatible endpoint.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from qdrant_client import AsyncQdrantClient
from qdrant_client.http.models import (
    Distance,
    FieldCondition,
    Filter,
    MatchValue,
    PointStruct,
    VectorParams,
)

logger = logging.getLogger(__name__)


@dataclass
class Convention:
    """A retrieved repository convention with its similarity score.

    Attributes:
        description: Human-readable description of the convention.
        convention_type: Category (naming, testing, error_handling, etc.).
        example_code: Optional code snippet illustrating the convention.
        confidence_score: Model confidence in this convention.
        similarity_score: Cosine similarity to the query (0.0–1.0).
    """

    description: str
    convention_type: str
    example_code: str | None
    confidence_score: float
    similarity_score: float


class QdrantConventionStore:
    """Async Qdrant client for repository convention vector storage and retrieval.

    Args:
        host: Qdrant server hostname.
        port: Qdrant server port.
        collection_name: Name of the Qdrant collection.
        embedding_dim: Dimensionality of the embedding vectors.
    """

    def __init__(
        self,
        host: str,
        port: int,
        collection_name: str,
        embedding_dim: int = 1536,
    ) -> None:
        self._client = AsyncQdrantClient(host=host, port=port)
        self._collection_name = collection_name
        self._embedding_dim = embedding_dim

    async def ensure_collection(self) -> None:
        """Create the collection if it does not already exist.

        Uses cosine distance for semantic similarity search.
        Called once at worker startup.
        """
        try:
            await self._client.get_collection(self._collection_name)
            logger.debug(
                "Qdrant collection already exists",
                extra={"collection": self._collection_name},
            )
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
                extra={"collection": self._collection_name, "dim": self._embedding_dim},
            )

    async def search_conventions(
        self,
        query_vector: list[float],
        repo_full_name: str,
        *,
        top_k: int = 5,
        score_threshold: float = 0.70,
    ) -> list[Convention]:
        """Search for relevant conventions using semantic similarity.

        Args:
            query_vector: Embedding of the review context query.
            repo_full_name: Repository to filter by (``owner/repo``).
            top_k: Maximum number of conventions to return.
            score_threshold: Minimum cosine similarity score (0.0–1.0).

        Returns:
            List of matching conventions sorted by similarity (highest first).
        """
        # Filter to only return conventions for this specific repository
        repo_filter = Filter(
            must=[
                FieldCondition(
                    key="repo_full_name",
                    match=MatchValue(value=repo_full_name),
                )
            ]
        )

        results = await self._client.search(
            collection_name=self._collection_name,
            query_vector=query_vector,
            query_filter=repo_filter,
            limit=top_k,
            score_threshold=score_threshold,
            with_payload=True,
        )

        conventions: list[Convention] = []
        for result in results:
            payload = result.payload or {}
            conventions.append(
                Convention(
                    description=payload.get("description", ""),
                    convention_type=payload.get("convention_type", "general"),
                    example_code=payload.get("example_code"),
                    confidence_score=payload.get("confidence_score", 0.7),
                    similarity_score=result.score,
                )
            )

        logger.debug(
            "Convention search complete",
            extra={
                "repo": repo_full_name,
                "results": len(conventions),
                "top_score": conventions[0].similarity_score if conventions else 0.0,
            },
        )
        return conventions

    async def upsert_convention(
        self,
        *,
        id: str,
        vector: list[float],
        repo_full_name: str,
        description: str,
        convention_type: str,
        example_code: str | None = None,
        confidence_score: float = 0.7,
        source_pr_number: int | None = None,
    ) -> None:
        """Upsert a convention into the vector store.

        Args:
            id: Unique UUID for this convention (for upsert idempotency).
            vector: Embedding vector for the convention description.
            repo_full_name: Repository this convention belongs to.
            description: Human-readable description of the convention.
            convention_type: Category label.
            example_code: Optional illustrative code snippet.
            confidence_score: Confidence in the convention (0.0–1.0).
            source_pr_number: PR number where this was observed.
        """
        point = PointStruct(
            id=id,
            vector=vector,
            payload={
                "repo_full_name": repo_full_name,
                "description": description,
                "convention_type": convention_type,
                "example_code": example_code,
                "confidence_score": confidence_score,
                "source_pr_number": source_pr_number,
            },
        )
        await self._client.upsert(
            collection_name=self._collection_name,
            points=[point],
        )
        logger.debug(
            "Upserted convention",
            extra={"repo": repo_full_name, "type": convention_type, "id": id},
        )

    async def close(self) -> None:
        """Close the Qdrant client connection."""
        await self._client.close()

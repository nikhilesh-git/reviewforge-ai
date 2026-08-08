"""Celery tasks for the learner service.

The ``learn_from_pr`` task is triggered when a PR is merged.
It extracts conventions from the diff and stores them in Qdrant.
"""

from __future__ import annotations

import asyncio
import logging
import uuid

import structlog
from celery import Task

from .celery_app import celery_app
from .config import get_settings

logger = structlog.get_logger(__name__)


def _run_async(coro):
    """Run an async coroutine from a sync Celery task."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@celery_app.task(
    name="app.tasks.learn_from_pr",
    bind=True,
    max_retries=2,
    default_retry_delay=120,
    queue="learn",
    acks_late=True,
)
def learn_from_pr(self: Task, event_data: dict) -> dict:
    """Extract conventions from a merged PR and store in Qdrant.

    Args:
        self: The Celery task instance.
        event_data: Serialized ``PRLearnRequestedEvent`` dictionary.

    Returns:
        Dict with repo, pr_number, and conventions_stored count.
    """
    from shared.domain.events import PRLearnRequestedEvent

    event = PRLearnRequestedEvent.model_validate(event_data)
    log = logger.bind(
        repo=event.repo_full_name,
        pr_number=event.pr_number,
    )

    log.info("Starting learn_from_pr task")

    try:
        result = _run_async(
            _learn(event=event, log=log)
        )

        log.info(
            "Learning task completed",
            conventions_stored=result.get("conventions_stored", 0),
        )
        return result

    except Exception as exc:
        log.error("Learning task failed", error=str(exc))

        if self.request.retries < self.max_retries:
            log.warning("Retrying learn_from_pr", retry=self.request.retries + 1)
            raise self.retry(exc=exc)

        return {
            "status": "failed",
            "error": str(exc),
            "repo_full_name": event.repo_full_name,
            "pr_number": event.pr_number,
            "conventions_stored": 0,
        }


async def _learn(*, event, log) -> dict:
    """Async implementation of the learning pipeline."""
    from datetime import datetime, timezone

    from langchain_openai import OpenAIEmbeddings

    from shared.domain.models import RepositoryConvention, RepositoryRef

    from .convention_extractor import ConventionExtractor
    from .embeddings import aembed_text as embed  # Free local embeddings
    from .qdrant_store import ConventionVectorStore

    settings = get_settings()
    UTC = timezone.utc

    # Initialize dependencies
    extractor = ConventionExtractor(
        openrouter_api_key=settings.openrouter_api_key,
        openrouter_base_url=settings.openrouter_base_url,
        model=settings.primary_model,
        max_tokens=settings.llm_max_tokens,
        temperature=settings.llm_temperature,
        timeout=settings.llm_request_timeout,
        github_pat=settings.github_pat,
        max_conventions_per_pr=settings.max_conventions_per_pr,
    )

    qdrant_store = ConventionVectorStore(
        host=settings.qdrant_host,
        port=settings.qdrant_port,
        collection_name=settings.qdrant_collection_name,
        embedding_dim=settings.qdrant_embedding_dim,
    )


    # Ensure Qdrant collection exists
    await qdrant_store.ensure_collection()

    # Extract conventions from the merged PR
    extracted = await extractor.extract_from_pr(
        repo_full_name=event.repo_full_name,
        pr_number=event.pr_number,
        head_sha=event.head_sha,
    )

    if not extracted:
        log.info("No conventions extracted from PR")
        return {
            "status": "completed",
            "repo_full_name": event.repo_full_name,
            "pr_number": event.pr_number,
            "conventions_stored": 0,
        }

    # Build domain models and store in Qdrant
    repository = RepositoryRef.from_full_name(event.repo_full_name)
    conventions_stored = 0

    for extracted_conv in extracted:
        # Create the embedding text
        embedding_text = extracted_conv.description
        if extracted_conv.example_code:
            embedding_text += f"\n\nExample:\n{extracted_conv.example_code}"

        # Generate embedding
        try:
            vector = await embed(embedding_text)
        except Exception as exc:
            log.warning(
                "Failed to embed convention — skipping",
                error=str(exc),
                convention=extracted_conv.description[:80],
            )
            continue

        # Create domain model
        convention = RepositoryConvention(
            repository=repository,
            convention_type=extracted_conv.convention_type,
            description=extracted_conv.description,
            example_code=extracted_conv.example_code,
            source_pr_number=event.pr_number,
            confidence_score=extracted_conv.confidence,
        )

        # Store in Qdrant
        try:
            await qdrant_store.upsert_convention(
                convention=convention,
                vector=vector,
            )
            conventions_stored += 1
        except Exception as exc:
            log.warning(
                "Failed to store convention in Qdrant",
                error=str(exc),
                convention_type=extracted_conv.convention_type,
            )

    await qdrant_store.close()

    return {
        "status": "completed",
        "repo_full_name": event.repo_full_name,
        "pr_number": event.pr_number,
        "conventions_extracted": len(extracted),
        "conventions_stored": conventions_stored,
    }

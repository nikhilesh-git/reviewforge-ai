"""Free, zero-cost text embeddings for convention storage and retrieval.

Instead of calling a paid API for embeddings, we use a lightweight local
approach: ``sentence-transformers`` with the ``all-MiniLM-L6-v2`` model.

Why this model?
- Completely free, runs locally on CPU (no GPU needed)
- ~22MB download on first use, cached thereafter
- 384-dimensional embeddings (we use this as our embedding dim)
- Good semantic similarity for code-related text
- Used by thousands of production systems

If ``sentence-transformers`` is not installed, we fall back to a
deterministic hash-based pseudo-embedding that ensures the Qdrant feature
degrades gracefully (reviews still work, just without convention context).
"""

from __future__ import annotations

import hashlib
import logging
import struct
from typing import Any

logger = logging.getLogger(__name__)

# Dimension for the MiniLM model (change QDRANT_EMBEDDING_DIM in .env to match)
MINILM_DIM = 384

_model: Any | None = None  # Lazy-loaded SentenceTransformer


def _load_model() -> Any | None:
    """Lazy-load the SentenceTransformer model on first call."""
    global _model  # noqa: PLW0603
    if _model is not None:
        return _model
    try:
        from sentence_transformers import SentenceTransformer

        logger.info("Loading local embedding model (all-MiniLM-L6-v2)...")
        _model = SentenceTransformer("all-MiniLM-L6-v2")
        logger.info("Local embedding model loaded (384-dim, runs on CPU)")
        return _model
    except ImportError:
        logger.warning(
            "sentence-transformers not installed — using hash-based fallback embeddings. "
            "Convention search quality will be limited. "
            "Install with: pip install sentence-transformers"
        )
        return None


def embed_text(text: str) -> list[float]:
    """Embed a text string into a float vector.

    Uses SentenceTransformer locally (free, no API).
    Falls back to deterministic hash embedding if not installed.

    Args:
        text: The text to embed.

    Returns:
        A float vector of length MINILM_DIM (384).
    """
    model = _load_model()
    if model is not None:
        vector = model.encode(text, normalize_embeddings=True)
        return vector.tolist()

    # Fallback: deterministic hash-based pseudo-embedding
    # This preserves repeatability (same text → same vector) but
    # won't have real semantic similarity. Better than crashing.
    return _hash_embed(text, dim=MINILM_DIM)


async def aembed_text(text: str) -> list[float]:
    """Async wrapper around embed_text (SentenceTransformer is sync).

    SentenceTransformer runs on CPU and is fast enough (<50ms for short texts)
    that we don't need true async here. For production at scale, run in an
    executor to avoid blocking the event loop.
    """
    import asyncio

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, embed_text, text)


def _hash_embed(text: str, dim: int = 384) -> list[float]:
    """Produce a deterministic pseudo-embedding from a hash.

    This is a last-resort fallback — not semantically meaningful,
    but at least repeatable and won't crash.
    """
    digest = hashlib.sha512(text.encode("utf-8")).digest()
    # Repeat digest to fill `dim` floats (each float is 4 bytes = 32 bytes per digest)
    needed_bytes = dim * 4
    repeated = (digest * ((needed_bytes // len(digest)) + 1))[:needed_bytes]
    raw = struct.unpack(f"{dim}f", repeated)
    # Normalise to unit vector
    magnitude = sum(x * x for x in raw) ** 0.5 or 1.0
    return [x / magnitude for x in raw]

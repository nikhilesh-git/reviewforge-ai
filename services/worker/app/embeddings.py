"""Free, zero-cost text embeddings for convention storage and retrieval.

Instead of calling a paid API for embeddings, we use a lightweight local
approach: FastEmbed with the ``BAAI/bge-small-en-v1.5`` model.

Why this model?
- Completely free, runs locally on CPU (no GPU needed) via ONNX
- Requires no heavy PyTorch dependencies, fitting in 512MB RAM constraints
- 384-dimensional embeddings (we use this as our embedding dim)
- Good semantic similarity for code-related text

If ``fastembed`` is not installed, we fall back to a
deterministic hash-based pseudo-embedding that ensures the Qdrant feature
degrades gracefully (reviews still work, just without convention context).
"""

from __future__ import annotations

import hashlib
import logging
import struct
from typing import Any

logger = logging.getLogger(__name__)

# Dimension for the BGE model (change QDRANT_EMBEDDING_DIM in .env to match)
MINILM_DIM = 384

_model: Any | None = None  # Lazy-loaded FastEmbedEmbeddings


def _load_model() -> Any | None:
    """Lazy-load the FastEmbed model on first call."""
    global _model  # noqa: PLW0603
    if _model is not None:
        return _model
    try:
        from langchain_community.embeddings.fastembed import FastEmbedEmbeddings

        logger.info("Loading FastEmbed model (BAAI/bge-small-en-v1.5)...")
        _model = FastEmbedEmbeddings(model_name="BAAI/bge-small-en-v1.5")
        logger.info("FastEmbed model loaded (384-dim, runs on CPU)")
        return _model
    except ImportError:
        logger.warning(
            "fastembed not installed — using hash-based fallback embeddings. "
            "Convention search quality will be limited. "
            "Install with: pip install fastembed langchain-community"
        )
        return None


def embed_text(text: str) -> list[float]:
    """Embed a text string into a float vector.

    Uses FastEmbed locally (free, no API).
    Falls back to deterministic hash embedding if not installed.

    Args:
        text: The text to embed.

    Returns:
        A float vector of length MINILM_DIM (384).
    """
    model = _load_model()
    if model is not None:
        return model.embed_query(text)

    # Fallback: deterministic hash-based pseudo-embedding
    # This preserves repeatability (same text → same vector) but
    # won't have real semantic similarity. Better than crashing.
    return _hash_embed(text, dim=MINILM_DIM)


async def aembed_text(text: str) -> list[float]:
    """Async wrapper around embed_text.

    FastEmbed runs on CPU via ONNX and is extremely fast. For production at scale,
    we run it in an executor to avoid blocking the event loop.
    """
    import asyncio

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, embed_text, text)


def _hash_embed(text: str, dim: int = 384) -> list[float]:
    """Deterministic hash-based fallback embedding (no NaN/Inf risk)."""
    import math
    digest = hashlib.sha512(text.encode("utf-8")).digest()
    needed_bytes = dim * 4
    repeated = (digest * ((needed_bytes // len(digest)) + 1))[:needed_bytes]
    # Use unsigned 32-bit ints (always finite) and scale to [-1, 1]
    raw = struct.unpack(f"{dim}I", repeated)
    scaled = [(x / 2_147_483_647.5) - 1.0 for x in raw]
    magnitude = math.sqrt(sum(x * x for x in scaled)) or 1.0
    return [x / magnitude for x in scaled]

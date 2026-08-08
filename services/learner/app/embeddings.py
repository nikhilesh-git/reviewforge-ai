"""Free, zero-cost text embeddings for the learner service.

Shared embedding module — same approach as the worker service.
Uses sentence-transformers locally (all-MiniLM-L6-v2, 384-dim).
No API key, no cost, runs on CPU.
"""

from __future__ import annotations

# Re-export from the shared local embedder pattern
import hashlib
import logging
import struct
from typing import Any

logger = logging.getLogger(__name__)

MINILM_DIM = 384

_model: Any | None = None


def _load_model() -> Any | None:
    global _model  # noqa: PLW0603
    if _model is not None:
        return _model
    try:
        from sentence_transformers import SentenceTransformer
        logger.info("Loading local embedding model (all-MiniLM-L6-v2)...")
        _model = SentenceTransformer("all-MiniLM-L6-v2")
        logger.info("Local embedding model loaded (384-dim)")
        return _model
    except ImportError:
        logger.warning(
            "sentence-transformers not installed — using hash-based fallback. "
            "Install with: pip install sentence-transformers"
        )
        return None


def embed_text(text: str) -> list[float]:
    """Embed text locally using sentence-transformers (free)."""
    model = _load_model()
    if model is not None:
        vector = model.encode(text, normalize_embeddings=True)
        return vector.tolist()
    return _hash_embed(text, dim=MINILM_DIM)


async def aembed_text(text: str) -> list[float]:
    """Async wrapper around embed_text."""
    import asyncio
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, embed_text, text)


def _hash_embed(text: str, dim: int = 384) -> list[float]:
    digest = hashlib.sha512(text.encode("utf-8")).digest()
    needed_bytes = dim * 4
    repeated = (digest * ((needed_bytes // len(digest)) + 1))[:needed_bytes]
    raw = struct.unpack(f"{dim}f", repeated)
    magnitude = sum(x * x for x in raw) ** 0.5 or 1.0
    return [x / magnitude for x in raw]

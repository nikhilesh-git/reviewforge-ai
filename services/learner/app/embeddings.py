"""Free, zero-cost text embeddings for the learner service.

Shared embedding module — same approach as the worker service.
Uses FastEmbed locally (BAAI/bge-small-en-v1.5, 384-dim).
No API key, no cost, runs on CPU via ONNX.
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
        from langchain_community.embeddings.fastembed import FastEmbedEmbeddings
        logger.info("Loading FastEmbed model (BAAI/bge-small-en-v1.5)...")
        _model = FastEmbedEmbeddings(model_name="BAAI/bge-small-en-v1.5")
        logger.info("FastEmbed model loaded (384-dim)")
        return _model
    except ImportError:
        logger.warning(
            "fastembed not installed — using hash-based fallback. "
            "Install with: pip install fastembed langchain-community"
        )
        return None


def embed_text(text: str) -> list[float]:
    """Embed text locally using FastEmbed (free)."""
    model = _load_model()
    if model is not None:
        return model.embed_query(text)
    return _hash_embed(text, dim=MINILM_DIM)


async def aembed_text(text: str) -> list[float]:
    """Async wrapper around embed_text."""
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

"""Text embedding provider with pluggable backends.

Supports:
- hash: deterministic hash-based (fallback, no model needed)
- ollama: Ollama embedding API (recommended for local use)
- sentence-transformers: local model (best quality, needs GPU)

Config via HCC_EMBEDDING_PROVIDER, HCC_EMBEDDING_MODEL, HCC_EMBEDDING_DIM.
"""

from __future__ import annotations

import hashlib
import math
import re
import os
from typing import Optional

__all__ = ["embed_text", "EMBEDDING_DIM"]

# Configuration
EMBEDDING_PROVIDER = os.getenv("HCC_EMBEDDING_PROVIDER", "hash")
EMBEDDING_MODEL = os.getenv("HCC_EMBEDDING_MODEL", "BAAI/bge-m3")
EMBEDDING_DIM = int(os.getenv("HCC_EMBEDDING_DIM", "1024"))
OLLAMA_BASE_URL = os.getenv("HCC_OLLAMA_URL", "http://localhost:11434")

_TOKEN_RE = re.compile(r"\w+")

# Cache for loaded model
_model_cache: dict = {}


def embed_text(text: str, dim: int = EMBEDDING_DIM) -> list[float]:
    """Embed text using the configured provider."""
    if EMBEDDING_PROVIDER == "ollama":
        return _embed_ollama(text)
    elif EMBEDDING_PROVIDER == "sentence-transformers":
        return _embed_sentence(text, dim)
    else:
        return _embed_hash(text, dim)


def _embed_hash(text: str, dim: int) -> list[float]:
    """Deterministic hash-based embedding (fallback)."""
    vector = [0.0] * dim
    tokens = _TOKEN_RE.findall(text.lower())
    for token in tokens:
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        index = int.from_bytes(digest[:2], "big") % dim
        sign = 1.0 if digest[2] & 1 else -1.0
        vector[index] += sign
    norm = math.sqrt(sum(c * c for c in vector))
    if norm > 0.0:
        vector = [c / norm for c in vector]
    return vector


def _embed_ollama(text: str) -> list[float]:
    """Embed using Ollama API."""
    import httpx

    try:
        resp = httpx.post(
            f"{OLLAMA_BASE_URL}/api/embeddings",
            json={"model": EMBEDDING_MODEL, "prompt": text},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()["embedding"]
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"Ollama embedding failed: {e}, falling back to hash")
        return _embed_hash(text, EMBEDDING_DIM)


def _embed_sentence(text: str, dim: int) -> list[float]:
    """Embed using sentence-transformers with bge-m3."""
    global _model_cache
    if "model" not in _model_cache:
        from sentence_transformers import SentenceTransformer
        _model_cache["model"] = SentenceTransformer(EMBEDDING_MODEL)
    model = _model_cache["model"]
    emb = model.encode(text, normalize_embeddings=True)
    return emb.tolist()

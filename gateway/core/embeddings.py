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

from dotenv import load_dotenv

# Unlike gateway.core.config.Settings (pydantic BaseSettings, which parses its
# own env_file), this module reads os.getenv() directly — nothing else in the
# process was loading .env into the real environment, so every HCC_EMBEDDING_*
# setting silently fell back to its hardcoded default (provider=hash) no
# matter what .env said. That's the actual root cause behind 体检报告's
# "Embedding 默认是 hash 后端" finding — this call is the fix, not just the
# .env value change. load_dotenv() no-ops quietly if no .env is found, and
# never overrides variables already set in the real environment.
load_dotenv()

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
    """Embed using Ollama API.

    P0-2 fix: on failure this now *raises* instead of silently returning a
    hash-space vector. The old fallback wrote a vector from a completely
    different embedding space into the same pgvector column that every other
    row treats as ollama-space — poisoning it so it could never again match
    semantically, with no marker to tell it apart. Worse, it defeated the
    null-embedding safety in MemoryService.create (which catches embed
    failures and stores NULL): create never saw the failure because this
    function swallowed it and handed back a plausible-looking vector.

    Callers that can tolerate a missing vector already catch this
    (MemoryService.create → stores NULL; hybrid_search → BM25-only). Let it
    propagate to them rather than corrupting the column. A deployment that
    genuinely wants the hash backend sets HCC_EMBEDDING_PROVIDER=hash, which
    routes here-around entirely.
    """
    import httpx

    resp = httpx.post(
        f"{OLLAMA_BASE_URL}/api/embeddings",
        json={"model": EMBEDDING_MODEL, "prompt": text},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["embedding"]


def _embed_sentence(text: str, dim: int) -> list[float]:
    """Embed using sentence-transformers with bge-m3."""
    global _model_cache
    if "model" not in _model_cache:
        from sentence_transformers import SentenceTransformer
        _model_cache["model"] = SentenceTransformer(EMBEDDING_MODEL)
    model = _model_cache["model"]
    emb = model.encode(text, normalize_embeddings=True)
    return emb.tolist()

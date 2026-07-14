"""Text embedding provider.

The gateway needs to turn free-text queries (and stored memory content) into
fixed-size vectors so they can be compared with pgvector's cosine distance.

This module ships a lightweight, dependency-free *deterministic* embedder so the
semantic-search endpoint works end-to-end out of the box. It is NOT a real
semantic model — it hashes token features into a normalized vector. Swap
``embed_text`` for a real provider (e.g. sentence-transformers, an Anthropic /
OpenAI embedding endpoint, or a local model) when wiring this into production;
keep the returned dimensionality equal to ``EMBEDDING_DIM``.
"""

from __future__ import annotations

import hashlib
import math
import re

from gateway.models import EMBEDDING_DIM

__all__ = ["embed_text", "EMBEDDING_DIM"]

_TOKEN_RE = re.compile(r"\w+")


def embed_text(text: str, dim: int = EMBEDDING_DIM) -> list[float]:
    """Return an L2-normalized ``dim``-dimensional embedding for ``text``.

    Deterministic placeholder implementation: each token is hashed into the
    vector space (the "hashing trick"), giving stable, comparable vectors
    without any external model. Replace with a real embedding model in prod.
    """
    vector = [0.0] * dim
    tokens = _TOKEN_RE.findall(text.lower())
    for token in tokens:
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        # Use two bytes for the bucket index and one for the sign.
        index = int.from_bytes(digest[:2], "big") % dim
        sign = 1.0 if digest[2] & 1 else -1.0
        vector[index] += sign

    norm = math.sqrt(sum(component * component for component in vector))
    if norm > 0.0:
        vector = [component / norm for component in vector]
    return vector

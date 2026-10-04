"""Near-duplicate folding + diversity selection for fused hybrid-search results.

Why this exists (phase4, 2026-09-28)
------------------------------------
``reciprocal_rank_fusion`` only looks at *rank position* in each branch, and the
same piece of content is routinely written more than once — the same
conversation logged by several runtimes/harvesters produces rows whose stored
embeddings are almost collinear. RRF happily promotes all of them, so the final
top-k can be filled by three or four copies of one memory while the genuinely
distinct, on-topic memories sit just below the cut. Measured on the reachable
11-query subset, this saturation is part of why hybrid recall (0.545) trailed
plain vector recall (0.727).

What it does
------------
After RRF fusion and ``_apply_recency_source_weighting`` (so "score" here is the
final weighted composite), and before the final truncation:

1. **Fold** candidates into clusters by *content cosine* over the already-stored
   embeddings (no extra model calls). Any candidate whose similarity to an
   existing cluster representative is ``>= threshold`` joins that cluster; the
   highest-scoring member is kept as the representative. Folded members are not
   thrown away — they are recorded on the representative's ``duplicates`` list
   (observable, and available for the diversity backfill).
2. **Diversify** (optional): if folding left fewer than ``limit`` representatives,
   fill the remaining slots with MMR over the folded pool
   (``lambda * relevance - (1 - lambda) * max_similarity_to_selected``) so the
   tail does not just re-add another copy of a cluster already on the page.

Folding is content-driven only: nothing here keys on ``id`` or ``source``.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

__all__ = [
    "apply_folding_and_diversity",
    "fold_near_duplicates",
    "cosine_similarity",
]


def _as_vector(value: Any) -> Optional[list[float]]:
    """Coerce a stored embedding (list / tuple / numpy array / pgvector text) to floats."""
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip().strip("[]")
        if not text:
            return None
        try:
            return [float(x) for x in text.split(",")]
        except ValueError:
            return None
    try:
        return [float(x) for x in value]
    except (TypeError, ValueError):
        return None


def cosine_similarity(a: Optional[list[float]], b: Optional[list[float]]) -> float:
    """Cosine similarity of two vectors; -1.0 when either is missing/degenerate."""
    if not a or not b or len(a) != len(b):
        return -1.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0.0 or nb <= 0.0:
        return -1.0
    return dot / ((na ** 0.5) * (nb ** 0.5))


def _embedding_of(item: dict) -> Optional[list[float]]:
    memory = item.get("memory")
    return _as_vector(getattr(memory, "embedding", None))


def fold_near_duplicates(fused: list[dict], threshold: float) -> list[dict]:
    """Cluster ``fused`` (already sorted best-first) by content cosine.

    Returns the cluster representatives in their original relative order, each
    carrying a ``duplicates`` list of the folded members. Representatives keep
    every field they arrived with; each folded entry is the original item plus
    ``duplicate_similarity``.
    """
    representatives: list[dict] = []
    for item in fused:
        item.setdefault("duplicates", [])
        vector = _embedding_of(item)
        best_rep: Optional[dict] = None
        best_similarity = -1.0
        if vector is not None:
            for rep in representatives:
                similarity = cosine_similarity(vector, _embedding_of(rep))
                if similarity > best_similarity:
                    best_similarity = similarity
                    best_rep = rep
        if best_rep is not None and best_similarity >= threshold:
            folded = dict(item)
            folded["duplicate_similarity"] = best_similarity
            best_rep["duplicates"].append(folded)
        else:
            representatives.append(item)
    return representatives


def _mmr_fill(selected: list[dict], pool: list[dict], limit: int, mmr_lambda: float) -> list[dict]:
    """Greedily append from ``pool`` to ``selected`` until ``limit`` via MMR."""
    if limit <= 0:
        return selected
    scores = [float(it.get("rrf_score", 0.0)) for it in (*selected, *pool)]
    top = max(scores) if scores else 0.0
    denom = top if top > 0 else 1.0

    while pool and len(selected) < limit:
        best_index = 0
        best_value = float("-inf")
        for index, candidate in enumerate(pool):
            relevance = float(candidate.get("rrf_score", 0.0)) / denom
            redundancy = 0.0
            candidate_vector = _embedding_of(candidate)
            if candidate_vector is not None:
                for chosen in selected:
                    similarity = cosine_similarity(candidate_vector, _embedding_of(chosen))
                    if similarity > redundancy:
                        redundancy = similarity
            value = mmr_lambda * relevance - (1.0 - mmr_lambda) * redundancy
            if value > best_value:
                best_value = value
                best_index = index
        selected.append(pool.pop(best_index))
    return selected


def apply_folding_and_diversity(
    fused: list[dict],
    *,
    limit: int,
    enabled: bool = True,
    threshold: float = 0.95,
    diversity_enabled: bool = True,
    mmr_lambda: float = 0.7,
) -> list[dict]:
    """Fold near-duplicates out of the ranked list, then optionally backfill with MMR.

    Returns a list of the representatives (and, when folding left fewer than
    ``limit``, MMR-selected tail items) still sorted best-first. When
    ``enabled`` is false or ``threshold <= 0`` the input is returned unchanged
    so callers get the pre-phase4 behaviour exactly.
    """
    if not fused or not enabled or threshold <= 0.0:
        return fused

    representatives = fold_near_duplicates(fused, threshold)
    if len(representatives) >= limit:
        return representatives

    folded = [entry for rep in representatives for entry in rep.get("duplicates", [])]
    if not folded:
        return representatives

    if diversity_enabled:
        return _mmr_fill(representatives, folded, limit, mmr_lambda)

    folded.sort(key=lambda it: float(it.get("rrf_score", 0.0)), reverse=True)
    return representatives + folded[: max(0, limit - len(representatives))]

"""Reciprocal Rank Fusion — shared by memory and document hybrid search.

Extracted out of ``gateway.services`` so :mod:`gateway.services.document_service`
can reuse the exact same fusion logic instead of forking a second copy.
"""

from __future__ import annotations

from typing import Any

# 60 is the value from the original RRF paper (Cormack et al. 2009) and what
# most hybrid-search implementations (incl. QMD) default to — it's not
# sensitive to tuning at our scale, so no env knob.
RRF_K = 60

__all__ = ["RRF_K", "reciprocal_rank_fusion"]


def reciprocal_rank_fusion(
    bm25_results: list[tuple[Any, float]],
    vector_results: list[tuple[Any, float]],
) -> list[dict]:
    """Merge two ranked result lists by Reciprocal Rank Fusion.

    RRF only looks at *rank position* within each list, not the raw scores —
    which sidesteps the "BM25 scores and cosine distances live on
    incomparable scales" problem entirely. score(doc) = sum over lists
    containing doc of 1/(k + rank), 1-indexed rank. Returns items sorted by
    descending fused score, each a dict carrying provenance from whichever
    branch(es) matched.

    Items are keyed by ``.id`` — works for any ORM row exposing that
    attribute (``Memory``, ``Document``), not just one model.
    """
    by_id: dict[str, dict] = {}

    for rank, (row, score) in enumerate(bm25_results, start=1):
        entry = by_id.setdefault(row.id, {"row": row, "rrf_score": 0.0})
        entry["rrf_score"] += 1.0 / (RRF_K + rank)
        entry["bm25_rank"] = rank
        entry["bm25_score"] = score

    for rank, (row, distance) in enumerate(vector_results, start=1):
        entry = by_id.setdefault(row.id, {"row": row, "rrf_score": 0.0})
        entry["rrf_score"] += 1.0 / (RRF_K + rank)
        entry["vector_rank"] = rank
        entry["vector_distance"] = distance

    return sorted(by_id.values(), key=lambda item: item["rrf_score"], reverse=True)

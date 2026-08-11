"""Memory graph: turns the flat ``memories`` table into a node/edge graph for
visualization (HanyanOS docs/14 三 记忆星空图 — borrowed the *idea* from
Ackem's d3 force-graph memory-viz, not its AGPL code — see that doc).

Node = one memory (size = importance, color = a lightweight keyword-derived
emotion estimate). Edge = association between two memories, of two kinds:

* ``semantic`` — pgvector cosine similarity between embeddings, capped to
  each node's top-``knn`` neighbors so the graph stays a set of local
  clusters instead of a hairball.
* ``temporal`` — consecutive memories of the same ``type`` created within
  ``temporal_window_hours`` of each other (adjacent-in-time chaining, e.g. a
  conversation session or a burst of related tool calls).

Memories carry no per-row emotion field (that lives in the separate, stateful
:class:`~core.emotion.EmotionEngine` — the "current mood", not a per-memory
tag). ``_estimate_affect`` reuses that module's keyword-trigger tables
(``EMOTION_TRIGGERS`` / ``NEW_DIM_TRIGGERS``) as a stateless one-shot scan
over a single memory's text, which is enough for a rough color/valence
signal without adding a new stored column or an LLM call per memory.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import Integer, and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from core.emotion import EMOTION_TRIGGERS, NEW_DIM_TRIGGERS, _is_negated
from gateway.models import Memory

__all__ = ["build_memory_graph"]

# Dims pulled toward "good" vs "bad" for the single valence scalar returned
# alongside the dominant dimension — not exhaustive (focus/arousal/shyness
# are left neutral), just enough to bias node color warm/cool.
_POSITIVE_DIMS = {
    "happiness", "closeness", "curiosity", "tenderness", "playfulness",
    "excitement", "ecstasy", "proud",
}
_NEGATIVE_DIMS = {
    "worry", "sadness", "anger", "anxiety", "loneliness", "jealousy", "fatigue",
}

# Dominant-dimension -> hex color, so any consumer (HUD widget, the bundled
# d3 viewer) can render immediately without re-implementing the 17-dim
# taxonomy. Picked for rough valence grouping (warm = positive, cool = low
# energy, red/purple = negative-arousal) rather than exact hue science.
DIMENSION_COLOR: dict[str, str] = {
    "happiness": "#F5C542", "closeness": "#F2789F", "tenderness": "#F2A6C6",
    "curiosity": "#4FB8AF", "playfulness": "#9BD65C", "excitement": "#F2884B",
    "ecstasy": "#E85D9C",
    "worry": "#E0A64E", "anxiety": "#9B6FD1", "sadness": "#5B87C5",
    "anger": "#D1495B", "jealousy": "#B14FD6", "loneliness": "#6C7A93",
    "fatigue": "#8B8B8B", "focus": "#4C6FD4", "arousal": "#D64F8A",
    "shyness": "#F0B7A4",
}
_DEFAULT_COLOR = "#7C7C88"  # no keyword hit — neutral gray

_AAF_TEXT_CHARS = 400  # truncate before the keyword scan; content can run long


def _estimate_affect(text: str) -> dict[str, Any]:
    """One-shot keyword scan (T3 table) over a single memory's text.

    Returns ``{"dominant": str|None, "valence": float, "color": str}``.
    Stateless — does not touch :class:`~core.emotion.EmotionEngine`, just
    reuses its trigger tables and negation check.
    """
    text_lower = (text or "")[:_AAF_TEXT_CHARS].lower()
    shifts: dict[str, float] = {}
    for keyword, dim_shifts in EMOTION_TRIGGERS.items():
        idx = text_lower.find(keyword)
        if idx == -1 or _is_negated(text_lower, idx):
            continue
        for dim, shift in dim_shifts.items():
            shifts[dim] = shifts.get(dim, 0.0) + shift
    for keyword, dim_shifts in NEW_DIM_TRIGGERS.items():
        idx = text_lower.find(keyword)
        if idx == -1 or _is_negated(text_lower, idx):
            continue
        for dim, shift in dim_shifts.items():
            shifts[dim] = shifts.get(dim, 0.0) + shift

    if not shifts:
        return {"dominant": None, "valence": 0.0, "color": _DEFAULT_COLOR}

    dominant = max(shifts, key=lambda d: abs(shifts[d]))
    valence = sum(v for d, v in shifts.items() if d in _POSITIVE_DIMS) - sum(
        v for d, v in shifts.items() if d in _NEGATIVE_DIMS
    )
    return {
        "dominant": dominant,
        "valence": round(max(-1.0, min(1.0, valence)), 3),
        "color": DIMENSION_COLOR.get(dominant, _DEFAULT_COLOR),
    }


async def build_memory_graph(
    session: AsyncSession,
    *,
    user_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    type: Optional[str] = None,
    limit: int = 200,
    knn: int = 5,
    min_similarity: float = 0.5,
    temporal_window_hours: float = 6.0,
) -> dict[str, Any]:
    """Build a node/edge graph over the ``limit`` most recent active memories.

    Two-pass: first fetch the node set (most recent, optionally scoped by
    user/agent/type), then compute edges restricted to that same id set —
    so cost stays bounded by ``limit`` regardless of total table size
    (currently ~2.7k rows).
    """
    limit = max(1, min(limit, 200))
    knn = max(0, min(knn, 20))

    stmt = select(Memory).where(Memory.status == "active")
    if user_id:
        stmt = stmt.where(Memory.user_id == user_id)
    if agent_id:
        stmt = stmt.where(Memory.agent_id == agent_id)
    if type:
        stmt = stmt.where(Memory.type == type)
    stmt = stmt.order_by(Memory.created_at.desc()).limit(limit)

    result = await session.execute(stmt)
    memories = list(result.scalars().all())

    nodes: list[dict[str, Any]] = []
    for m in memories:
        affect = _estimate_affect(f"{m.content}\n{m.summary or ''}")
        preview_src = (m.summary or m.content or "").strip().replace("\n", " ")
        nodes.append({
            "id": m.id,
            "label": preview_src[:60] + ("…" if len(preview_src) > 60 else ""),
            "preview": preview_src[:200] + ("…" if len(preview_src) > 200 else ""),
            "type": m.type,
            "importance": m.importance,
            "tags": m.tags or [],
            "access_count": m.access_count,
            "created_at": m.created_at.isoformat() if m.created_at else None,
            "has_embedding": m.embedding is not None,
            "emotion": affect,
        })

    ids = [m.id for m in memories if m.embedding is not None]
    edges: list[dict[str, Any]] = []
    edge_seen: dict[frozenset, dict[str, Any]] = {}

    if len(ids) >= 2 and knn > 0:
        a = aliased(Memory)
        b = aliased(Memory)
        similarity = (1 - a.embedding.cosine_distance(b.embedding)).label("similarity")
        rank = (
            func.row_number()
            .over(partition_by=a.id, order_by=similarity.desc())
            .cast(Integer)
            .label("rn")
        )
        pair_stmt = (
            select(a.id.label("source_id"), b.id.label("target_id"), similarity, rank)
            .where(
                and_(
                    a.id.in_(ids),
                    b.id.in_(ids),
                    a.id != b.id,
                )
            )
        )
        ranked = pair_stmt.subquery()
        final_stmt = (
            select(ranked.c.source_id, ranked.c.target_id, ranked.c.similarity)
            .where(ranked.c.rn <= knn, ranked.c.similarity >= min_similarity)
        )
        pair_result = await session.execute(final_stmt)
        for source_id, target_id, sim in pair_result.all():
            key = frozenset((source_id, target_id))
            sim = float(sim)
            existing = edge_seen.get(key)
            if existing is None or sim > existing["weight"]:
                edge_seen[key] = {
                    "source": source_id, "target": target_id,
                    "type": "semantic", "weight": round(sim, 4),
                }
        edges.extend(edge_seen.values())

    # Temporal edges: consecutive same-type memories within the time window.
    # `memories` is already created_at DESC; group-by-type preserves that
    # order so "consecutive" means "adjacent in time within that type".
    by_type: dict[str, list[Memory]] = {}
    for m in memories:
        by_type.setdefault(m.type, []).append(m)

    window_seconds = temporal_window_hours * 3600
    temporal_seen: set[frozenset] = set()
    for group in by_type.values():
        for earlier, later in zip(group, group[1:]):
            if earlier.created_at is None or later.created_at is None:
                continue
            delta = (earlier.created_at - later.created_at).total_seconds()
            if delta > window_seconds:
                continue
            key = frozenset((earlier.id, later.id))
            if key in edge_seen or key in temporal_seen:
                continue
            temporal_seen.add(key)
            edges.append({
                "source": later.id, "target": earlier.id,
                "type": "temporal", "weight": 1.0,
            })

    return {
        "nodes": nodes,
        "edges": edges,
        "meta": {
            "node_count": len(nodes),
            "edge_count": len(edges),
            "semantic_edge_count": len(edge_seen),
            "temporal_edge_count": len(temporal_seen),
            "params": {
                "user_id": user_id, "agent_id": agent_id, "type": type,
                "limit": limit, "knn": knn, "min_similarity": min_similarity,
                "temporal_window_hours": temporal_window_hours,
            },
        },
    }

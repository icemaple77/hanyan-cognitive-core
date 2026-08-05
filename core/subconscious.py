"""Subconscious — three-layer retrieval system.

Implements the conscious → preconscious → subconscious hierarchy:
- Conscious: current conversation context
- Preconscious: frequently accessed, recent memories
- Subconscious: long-term, rarely accessed but potentially relevant knowledge

When queried, all three layers are searched and merged with RRF scoring.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from core.config import core_settings
from core.emotion import get_emotion_engine

logger = logging.getLogger(__name__)

RRF_K = 60  # RRF constant

# Mood-congruent retrieval weighting (docs/emotion-design.md 2.3) — this is
# not "let emotion distort relevance", it's a small tie-breaking nudge that
# mirrors the real mood-congruent-memory effect: candidates tagged as
# intimate/concerning get a slight boost only when the current emotional
# state already leans that way. Kept deliberately small (see
# HCC_EMOTION_RETRIEVAL_*_WEIGHT defaults, 0.10-0.15) so it never overrides
# semantic relevance.
_INTIMACY_TAGS = {
    "亲密", "含烟", "爱", "关心", "陪伴", "喜欢", "想念", "谢谢",
    "closeness", "love", "intimacy", "affection",
}
_WORRY_TAGS = {
    "问题", "异常", "故障", "错误", "风险", "紧急", "bug",
    "issue", "error", "worry", "incident", "urgent",
}


def _emotion_weight(tags: list[Any], emotion_state: dict[str, float]) -> float:
    """Mood-congruent RRF weight (0, or one/both of the 2.3 bonuses)."""
    if not emotion_state:
        return 0.0
    tagset = {str(t).lower() for t in tags}
    weight = 0.0
    if (
        emotion_state.get("closeness", 0.0) > core_settings.emotion_retrieval_closeness_threshold
        and tagset & _INTIMACY_TAGS
    ):
        weight += core_settings.emotion_retrieval_closeness_weight
    if (
        emotion_state.get("worry", 0.0) > core_settings.emotion_retrieval_worry_threshold
        and tagset & _WORRY_TAGS
    ):
        weight += core_settings.emotion_retrieval_worry_weight
    return weight


@dataclass
class SubconsciousResult:
    """A result from the subconscious retrieval."""
    content: str
    source: str  # conscious / preconscious / subconscious
    score: float
    memory_id: str | None = None
    tags: list[str] = field(default_factory=list)
    importance: float = 0.5
    last_access_days: float = 0.0


class Subconscious:
    """Three-layer memory retrieval system.

    Conscious: what's happening right now (in-memory, current session)
    Preconscious: what's been recently accessed (last N hours)
    Subconscious: long-term archive (high forget score, high potential relevance)
    """

    def __init__(self):
        self._conscious: list[dict[str, Any]] = []  # Current session context
        self._max_conscious = 20  # Max items in conscious

    def add_to_conscious(self, content: str, source: str = "conversation",
                         metadata: dict | None = None) -> None:
        """Add content to conscious layer (current session)."""
        entry = {
            "content": content,
            "source": source,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "metadata": metadata or {},
        }
        self._conscious.append(entry)
        if len(self._conscious) > self._max_conscious:
            self._conscious = self._conscious[-self._max_conscious:]
        logger.debug("subconscious: added to conscious (%d items)", len(self._conscious))

    def get_conscious(self, limit: int = 10) -> list[dict[str, Any]]:
        """Get recent conscious content."""
        return self._conscious[-limit:]

    async def retrieve(self, query: str, memory_provider: Any = None,
                       limit: int = 10, user_id: str | None = None,
                       agent_id: str | None = None,
                       emotion_state: dict[str, float] | None = None) -> list[SubconsciousResult]:
        """Three-layer retrieval.

        Parameters
        ----------
        query: str — The search query
        memory_provider: optional — Memory provider for searching subconscious
        limit: int — Max results
        user_id: optional — Filter by user
        emotion_state: optional — 6-dim state for mood-congruent RRF weighting
            (docs/emotion-design.md 2.3). Defaults to the live EmotionEngine
            state when omitted.

        Returns
        -------
        List of SubconsciousResult sorted by RRF score.
        """
        if emotion_state is None:
            emotion_state = get_emotion_engine().state
        results: list[SubconsciousResult] = []

        # Layer 1: Conscious — current session
        for entry in self._conscious[-limit:]:
            content = entry.get("content", "")
            if query.lower() in content.lower():
                results.append(SubconsciousResult(
                    content=content[:200],
                    source="conscious",
                    score=1.0,
                    tags=[],
                    importance=0.8,
                    last_access_days=0,
                ))

        # Layer 2: Preconscious — search memory provider for recent/important items
        if memory_provider:
            try:
                preconscious = await memory_provider.search(
                    query=query, user_id=user_id, agent_id=agent_id, limit=limit * 2
                )
                items = preconscious.get("items", []) if isinstance(preconscious, dict) else (preconscious if isinstance(preconscious, list) else [])
                for item in items:
                    memory_id = item.get("id") if isinstance(item, dict) else getattr(item, "id", None)
                    content = item.get("content") if isinstance(item, dict) else getattr(item, "content", "")
                    importance = item.get("importance", 0.5) if isinstance(item, dict) else getattr(item, "importance", 0.5)
                    tags = item.get("tags", []) if isinstance(item, dict) else list(getattr(item, "tags", []) or [])
                    days_since = 0.0
                    results.append(SubconsciousResult(
                        content=str(content)[:200],
                        source="preconscious",
                        score=0.7,
                        memory_id=str(memory_id) if memory_id else None,
                        tags=list(tags) if tags else [],
                        importance=float(importance),
                        last_access_days=days_since,
                    ))
            except Exception as e:
                logger.warning("subconscious preconscious search failed: %s", e)

        # Layer 3: Subconscious — search with higher decay tolerance
        if memory_provider:
            try:
                subconscious = await memory_provider.search(
                    query=query, user_id=user_id, agent_id=agent_id, limit=limit
                )
                items = subconscious.get("items", []) if isinstance(subconscious, dict) else (subconscious if isinstance(subconscious, list) else [])
                for item in items:
                    memory_id = item.get("id") if isinstance(item, dict) else getattr(item, "id", None)
                    content = item.get("content") if isinstance(item, dict) else getattr(item, "content", "")
                    importance = item.get("importance", 0.5) if isinstance(item, dict) else getattr(item, "importance", 0.5)
                    tags = item.get("tags", []) if isinstance(item, dict) else list(getattr(item, "tags", []) or [])
                    results.append(SubconsciousResult(
                        content=str(content)[:200],
                        source="subconscious",
                        score=0.4,
                        memory_id=str(memory_id) if memory_id else None,
                        tags=list(tags) if tags else [],
                        importance=float(importance),
                        last_access_days=30.0,
                    ))
            except Exception as e:
                logger.warning("subconscious deep search failed: %s", e)

        # Merge with RRF scoring
        return self._rrf_merge(results, limit, emotion_state)

    def _rrf_merge(
        self, results: list[SubconsciousResult], limit: int, emotion_state: dict[str, float] | None = None
    ) -> list[SubconsciousResult]:
        """Reciprocal Rank Fusion merge with dedup + mood-congruent weighting (2.3)."""
        seen_content: set[str] = set()
        ranked: list[SubconsciousResult] = []

        # Group by source to assign ranks
        by_source: dict[str, list[SubconsciousResult]] = {}
        for r in results:
            by_source.setdefault(r.source, []).append(r)

        for source_rank, (source, items) in enumerate(by_source.items()):
            for item_rank, item in enumerate(items):
                score = sum(
                    (1.0 / (RRF_K + source_rank + 1))
                    for _ in range(1)
                ) + (1.0 / (RRF_K + item_rank + 1))
                score *= 1 + _emotion_weight(item.tags, emotion_state or {})
                item.score = round(score, 4)

        # Sort by score descending, dedup
        all_sorted = sorted(results, key=lambda r: r.score, reverse=True)
        for r in all_sorted:
            key = r.content[:80].strip().lower()
            if key and key not in seen_content:
                seen_content.add(key)
                ranked.append(r)
            if len(ranked) >= limit:
                break

        return ranked

    def get_conscious_context(self, max_chars: int = 2000) -> str:
        """Format conscious context for prompt injection."""
        parts = []
        for entry in self._conscious[-10:]:
            content = entry.get("content", "")
            if content:
                parts.append(f"[{entry.get('source', 'chat')}] {content[:200]}")
        text = "\n".join(parts)
        return text[:max_chars]

    def clear_conscious(self) -> None:
        """Clear current session context."""
        self._conscious = []


# Singleton
_subconscious: Subconscious | None = None


def get_subconscious() -> Subconscious:
    global _subconscious
    if _subconscious is None:
        _subconscious = Subconscious()
    return _subconscious

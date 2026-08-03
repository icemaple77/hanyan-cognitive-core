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

logger = logging.getLogger(__name__)

RRF_K = 60  # RRF constant


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
                       agent_id: str | None = None) -> list[SubconsciousResult]:
        """Three-layer retrieval.

        Parameters
        ----------
        query: str — The search query
        memory_provider: optional — Memory provider for searching subconscious
        limit: int — Max results
        user_id: optional — Filter by user

        Returns
        -------
        List of SubconsciousResult sorted by RRF score.
        """
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
        return self._rrf_merge(results, limit)

    def _rrf_merge(self, results: list[SubconsciousResult], limit: int) -> list[SubconsciousResult]:
        """Reciprocal Rank Fusion merge with dedup."""
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

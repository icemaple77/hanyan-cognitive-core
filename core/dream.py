"""Dream Engine — nightly memory consolidation.

Clusters similar memories, merges duplicates, extracts patterns,
generates knowledge entries, and updates emotional baselines.
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


class DreamEngine:
    """Nightly memory consolidation engine.

    Processes memories from PostgreSQL:
    1. Cluster similar memories by content/tags
    2. Merge duplicates (keep highest importance)
    3. Extract patterns across clusters
    4. Generate knowledge summaries
    5. Update emotion baseline
    """

    def __init__(self):
        self._session = None

    async def consolidate(self, memories: list[dict[str, Any]]) -> dict[str, Any]:
        """Run one consolidation cycle on the given memories.

        Parameters
        ----------
        memories:
            List of memory dicts with keys: id, content, summary, importance,
            tags, created_at, updated_at.

        Returns
        -------
        dict with consolidation results.
        """
        if not memories:
            return {"clusters": 0, "merged": 0, "patterns": [], "knowledge": []}

        # 1. Cluster by tag overlap
        clusters = self._cluster_by_tags(memories)

        # 2. Find duplicates within clusters
        merged_count = 0
        patterns = []
        knowledge = []

        for cluster in clusters:
            # Merge near-duplicates
            kept = self._merge_duplicates(cluster)
            merged_count += len(cluster) - len(kept)

            # Extract patterns
            cluster_patterns = self._extract_patterns(kept)
            patterns.extend(cluster_patterns)

            # Generate knowledge
            if len(kept) >= 2:
                summary = self._generate_knowledge(kept)
                if summary:
                    knowledge.append(summary)

        return {
            "clusters": len(clusters),
            "merged": merged_count,
            "patterns": patterns[:10],
            "knowledge": knowledge[:5],
            "processed_memories": len(memories),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    def _cluster_by_tags(self, memories: list[dict]) -> list[list[dict]]:
        """Group memories by shared tags."""
        clusters: list[list[dict]] = []
        assigned = set()

        for i, m1 in enumerate(memories):
            if i in assigned:
                continue
            cluster = [m1]
            assigned.add(i)
            tags1 = set(m1.get("tags", []))

            for j, m2 in enumerate(memories):
                if j in assigned:
                    continue
                tags2 = set(m2.get("tags", []))
                if tags1 & tags2:  # Shared tags = same cluster
                    cluster.append(m2)
                    assigned.add(j)

            if len(cluster) >= 2:
                clusters.append(cluster)
            else:
                clusters.append(cluster)  # Lone memories still get recorded

        return clusters

    def _merge_duplicates(self, cluster: list[dict]) -> list[dict]:
        """Merge near-duplicate memories (similar content, same tags)."""
        kept = []
        seen_content = set()

        for mem in sorted(cluster, key=lambda m: m.get("importance", 0), reverse=True):
            # Simple content fingerprint: first 100 chars
            fingerprint = mem.get("content", "")[:100].strip().lower()
            if fingerprint not in seen_content:
                seen_content.add(fingerprint)
                kept.append(mem)

        return kept

    def _extract_patterns(self, cluster: list[dict]) -> list[dict]:
        """Extract recurring themes from a cluster of memories."""
        if len(cluster) < 2:
            return []

        # Count tag frequency
        all_tags = [t for m in cluster for t in m.get("tags", [])]
        tag_counts = Counter(all_tags)

        # Common topics
        common_tags = [tag for tag, count in tag_counts.most_common(3) if count >= 2]

        themes = []
        if common_tags:
            themes.append({
                "tags": common_tags,
                "frequency": len(cluster),
                "avg_importance": sum(m.get("importance", 0.5) for m in cluster) / len(cluster),
            })

        return themes

    def _generate_knowledge(self, cluster: list[dict]) -> dict | None:
        """Generate a knowledge summary from a cluster of related memories."""
        if not cluster:
            return None

        top = max(cluster, key=lambda m: m.get("importance", 0))
        tags = list(set(t for m in cluster for t in m.get("tags", [])))

        return {
            "title": top.get("summary") or top.get("content", "")[:80],
            "summary": f"Consolidated from {len(cluster)} related memories",
            "source_memories": [m.get("id") for m in cluster[:5]],
            "tags": tags[:5],
            "importance": min(1.0, sum(m.get("importance", 0.5) for m in cluster) / len(cluster) * 1.2),
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }


# Singleton
_dream_engine: DreamEngine | None = None


def get_dream_engine() -> DreamEngine:
    global _dream_engine
    if _dream_engine is None:
        _dream_engine = DreamEngine()
    return _dream_engine

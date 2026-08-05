"""Dream Engine — nightly memory consolidation.

Two generations of logic live in this module side by side:

* :meth:`DreamEngine.consolidate` — the original v1 single-stage engine
  (cluster by tag overlap -> merge near-duplicates -> extract patterns ->
  generate knowledge). Kept byte-for-byte behaviourally unchanged because the
  existing ``POST /api/v1/dream/consolidate`` endpoint
  (``gateway/api/cognitive_routes.py``) is already being called by external
  clients — changing its response shape or promotion behaviour out from under
  them is out of scope here.
* :meth:`DreamEngine.run_light` / :meth:`run_rem` / :meth:`run_deep` — the
  HCC-native three-phase pipeline from ``docs/dreaming-design.md`` (P0).
  These are additive: a new surface (``gateway/api/dream_routes.py``), not a
  replacement for the legacy endpoint. Only Deep writes to the durable
  ``Memory`` table; Light/REM only append to the auxiliary ``dream_signals``
  table, mirroring the doc's "only Deep writes MEMORY.md" principle.

Idempotency for Deep (and REM) is a ``dream_runs`` "did we already finish a
run today" guard, checked *before* any work starts. That is the actual fix
for the "今夜无梦" x3 bug from ``AICore/Dreams/DREAMS-2026-08-04.md``: the bug
was two independent trigger paths (the OpenClaw plugin's ``setInterval`` +
its manual ``memory_dreaming`` tool) racing to both write "no dream" the same
night. Here there is exactly one code path per phase, and it self-guards
against being re-entered the same calendar day even if triggered twice
(cron + manual retry, a restart, etc.) — see ``_latest_run_today``.
"""

from __future__ import annotations

import hashlib
import logging
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from core.config import CoreSettings, core_settings
from core.dream_narrative import write_dream_diary
from core.emotion import get_emotion_engine
from core.event_bus import EventBus, EventType
from core.personality import get_personality_engine
from gateway.core.database import async_session
from gateway.core.events import get_event_bus
from gateway.models import DreamRun, DreamSignal, EmotionSnapshot, Memory, MemoryStatus

logger = logging.getLogger(__name__)


class DreamEngine:
    """Nightly memory consolidation engine.

    v1 API (``consolidate``) processes an in-memory list of memory dicts and
    is stateless. v2 API (``run_light``/``run_rem``/``run_deep``) owns its own
    DB session per call via ``session_factory`` and is stateful (reads/writes
    ``dream_signals``/``dream_runs``/``Memory``), matching the pattern already
    used by :class:`~core.qmd_generator.QMDGenerator` and
    :class:`~core.sync_engine.SyncEngine`.
    """

    def __init__(
        self,
        *,
        settings: CoreSettings | None = None,
        session_factory: async_sessionmaker | None = None,
        event_bus: EventBus | None = None,
    ) -> None:
        self._session = None  # legacy attribute, unused but kept for compat
        self._settings = settings or core_settings
        self._session_factory = session_factory or async_session
        self._event_bus = event_bus

    # ==================================================================
    # v1 legacy — unchanged behaviour, backs POST /api/v1/dream/consolidate
    # ==================================================================
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

    # ==================================================================
    # v2 native three-phase pipeline (docs/dreaming-design.md)
    # ==================================================================

    @staticmethod
    def _title(memory: Memory) -> str:
        summary = (memory.summary or "").strip()
        if summary:
            return summary.splitlines()[0][:100]
        content = (memory.content or "").strip()
        if content:
            return content.splitlines()[0][:100]
        return f"Memory {memory.id[:8]}"

    @staticmethod
    def _cosine(a: Any, b: Any) -> float | None:
        try:
            import numpy as np

            va, vb = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
            na, nb = float(np.linalg.norm(va)), float(np.linalg.norm(vb))
            if na == 0.0 or nb == 0.0:
                return None
            return float(np.dot(va, vb) / (na * nb))
        except Exception:  # noqa: BLE001 - malformed/missing vector must not crash a run
            return None

    def _dedupe_by_embedding(self, memories: list[Memory], *, threshold: float) -> list[Memory]:
        """Drop memories whose embedding cosine-matches an already-kept one.

        Memories without an embedding (still common — see
        ``scripts/index_documents.py`` docstring on the hash-placeholder
        provider) are always kept: no embedding means no basis for judging
        them a duplicate, so Light errs toward recording the signal.
        """
        kept: list[Memory] = []
        kept_vecs: list[Any] = []
        for m in memories:
            vec = m.embedding
            if vec is None:
                kept.append(m)
                continue
            is_dup = any(
                (sim := self._cosine(vec, kv)) is not None and sim >= threshold for kv in kept_vecs
            )
            if not is_dup:
                kept.append(m)
                kept_vecs.append(vec)
        return kept

    async def _existing_signal_memory_ids(self, session, phase: str, day) -> set[str]:
        result = await session.execute(
            select(DreamSignal.memory_id)
            .where(DreamSignal.phase == phase)
            .where(func.date(DreamSignal.created_at) == day)
        )
        return set(result.scalars().all())

    async def _latest_run_today(self, session, phase: str) -> DreamRun | None:
        today = datetime.now(timezone.utc).date()
        result = await session.execute(
            select(DreamRun)
            .where(DreamRun.phase == phase)
            .where(func.date(DreamRun.started_at) == today)
            .where(DreamRun.finished_at.isnot(None))
            .order_by(DreamRun.started_at.desc())
            .limit(1)
        )
        return result.scalars().first()

    # ------------------------------------------------------------------
    # Light — every dream_light_interval_hours
    # ------------------------------------------------------------------
    async def run_light(self, *, force: bool = False) -> dict[str, Any]:
        """Scan recently-created active memories, dedupe by embedding, record signals."""
        started = datetime.now(timezone.utc).replace(tzinfo=None)
        async with self._session_factory() as session:
            cutoff = started - timedelta(hours=self._settings.dream_light_lookback_hours)
            result = await session.execute(
                select(Memory).where(Memory.status == MemoryStatus.ACTIVE).where(Memory.created_at >= cutoff)
            )
            memories = list(result.scalars().all())

            existing = await self._existing_signal_memory_ids(session, "light", started.date())
            pending = [m for m in memories if m.id not in existing]
            kept = self._dedupe_by_embedding(pending, threshold=0.9)

            for m in kept:
                session.add(DreamSignal(memory_id=m.id, phase="light", boost=0.05, created_at=started))

            finished = datetime.now(timezone.utc).replace(tzinfo=None)
            stats = {
                "scanned": len(memories),
                "already_signalled_today": len(memories) - len(pending),
                "deduped": len(pending) - len(kept),
                "signals_added": len(kept),
            }
            session.add(DreamRun(phase="light", started_at=started, finished_at=finished, stats=stats))
            await session.commit()
            logger.info("run_light complete: %s", stats)
            return stats

    # ------------------------------------------------------------------
    # REM — daily, dream_rem_hour:dream_rem_minute
    # ------------------------------------------------------------------
    def _cluster_by_tag_overlap(self, memories: list[Memory]) -> list[list[Memory]]:
        clusters: list[list[Memory]] = []
        assigned: set[str] = set()
        by_id = {m.id: m for m in memories}
        ids = list(by_id)

        for i, id1 in enumerate(ids):
            if id1 in assigned:
                continue
            m1 = by_id[id1]
            tags1 = set(m1.tags or [])
            cluster = [m1]
            assigned.add(id1)
            for id2 in ids[i + 1:]:
                if id2 in assigned:
                    continue
                m2 = by_id[id2]
                if tags1 & set(m2.tags or []):
                    cluster.append(m2)
                    assigned.add(id2)
            clusters.append(cluster)
        return clusters

    async def run_rem(self, *, force: bool = False) -> dict[str, Any]:
        """Cluster the last N days of memories by tag overlap; record theme signals.

        Semantic (embedding) clustering is P3 in docs/dreaming-design.md —
        tag overlap is the documented P0 default for REM.
        """
        started = datetime.now(timezone.utc).replace(tzinfo=None)
        async with self._session_factory() as session:
            if not force:
                prior = await self._latest_run_today(session, "rem")
                if prior is not None:
                    logger.info("run_rem: already ran today (run_id=%s), skipping", prior.id)
                    return {**prior.stats, "skipped": True, "reason": "already_ran_today", "run_id": prior.id}

            cutoff = started - timedelta(days=self._settings.dream_rem_lookback_days)
            result = await session.execute(
                select(Memory).where(Memory.status == MemoryStatus.ACTIVE).where(Memory.created_at >= cutoff)
            )
            memories = [m for m in result.scalars().all() if m.tags]

            clusters = self._cluster_by_tag_overlap(memories)
            min_size = self._settings.dream_rem_min_cluster_size
            big_clusters = [c for c in clusters if len(c) >= min_size]

            existing = await self._existing_signal_memory_ids(session, "rem", started.date())
            signals_added = 0
            cluster_summaries: list[dict[str, Any]] = []
            for cluster in big_clusters:
                tag_counts = Counter(t for m in cluster for t in (m.tags or []))
                top_tag = tag_counts.most_common(1)[0][0] if tag_counts else "未命名主题"
                cluster_summaries.append(
                    {"tag": top_tag, "size": len(cluster), "memory_ids": [m.id for m in cluster]}
                )
                for m in cluster:
                    if m.id in existing:
                        continue
                    session.add(
                        DreamSignal(
                            memory_id=m.id, phase="rem", boost=0.08, cluster_tag=top_tag, created_at=started
                        )
                    )
                    existing.add(m.id)
                    signals_added += 1

            finished = datetime.now(timezone.utc).replace(tzinfo=None)
            stats = {
                "scanned": len(memories),
                "clusters": len(big_clusters),
                "signals_added": signals_added,
                "cluster_summaries": cluster_summaries,
            }
            session.add(DreamRun(phase="rem", started_at=started, finished_at=finished, stats=stats))
            await session.commit()
            logger.info("run_rem complete: scanned=%s clusters=%s", stats["scanned"], stats["clusters"])
            return stats

    async def _todays_rem_clusters(self, session) -> list[dict[str, Any]]:
        run = await self._latest_run_today(session, "rem")
        if run and run.stats:
            return run.stats.get("cluster_summaries", [])
        return []

    async def _recent_cluster_tags(self, session) -> dict[str, str]:
        """memory_id -> most-recent REM cluster_tag within the REM lookback window."""
        cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(
            days=self._settings.dream_rem_lookback_days
        )
        result = await session.execute(
            select(DreamSignal)
            .where(DreamSignal.phase == "rem")
            .where(DreamSignal.created_at >= cutoff)
            .order_by(DreamSignal.created_at)
        )
        mapping: dict[str, str] = {}
        for sig in result.scalars().all():
            if sig.cluster_tag:
                mapping[sig.memory_id] = sig.cluster_tag
        return mapping

    # ------------------------------------------------------------------
    # Deep — daily, dream_deep_hour:dream_deep_minute. Only phase that
    # writes to Memory. Score formula: docs/dreaming-design.md 2.4 (Phase 1).
    # ------------------------------------------------------------------
    def _score_memory(self, memory: Memory, signals: list[DreamSignal], now: datetime) -> dict[str, Any]:
        halflife = self._settings.dream_recency_halflife_days
        created = memory.created_at or now
        last_access = memory.last_access or created
        age_days = max(0.0, (now - created).total_seconds() / 86400)
        recency_age_days = max(0.0, (now - last_access).total_seconds() / 86400)

        recency = 2 ** (-recency_age_days / halflife)
        frequency = min((memory.access_count or 0) / 10.0, 1.0)

        distinct_days = {s.created_at.date() for s in signals if s.created_at}
        consolidation = (memory.importance or 0.0) * 0.7 + min(len(distinct_days) / 5.0, 1.0) * 0.3

        conceptual = min(len(memory.tags or []) / 5.0, 1.0)

        phase_boost = sum(
            s.boost * (2 ** (-max(0.0, (now - s.created_at).total_seconds() / 86400) / halflife))
            for s in signals
            if s.created_at and (now - s.created_at).days <= 14
        )

        score = 0.30 * recency + 0.28 * frequency + 0.22 * consolidation + 0.12 * conceptual + phase_boost

        return {
            "score": round(score, 4),
            "recency": round(recency, 4),
            "frequency": round(frequency, 4),
            "consolidation": round(consolidation, 4),
            "conceptual": round(conceptual, 4),
            "phase_boost": round(phase_boost, 4),
            "age_days": round(age_days, 2),
        }

    async def _fetch_signals_by_memory(self, session, memory_ids: list[str]) -> dict[str, list[DreamSignal]]:
        if not memory_ids:
            return {}
        result = await session.execute(select(DreamSignal).where(DreamSignal.memory_id.in_(memory_ids)))
        out: dict[str, list[DreamSignal]] = defaultdict(list)
        for sig in result.scalars().all():
            out[sig.memory_id].append(sig)
        return out

    def _group_for_knowledge(
        self, memories: list[Memory], cluster_map: dict[str, str]
    ) -> dict[str, list[Memory]]:
        """Group promoted memories into knowledge clusters.

        Prefer the REM-assigned cluster_tag (semantically meaningful, already
        vetted by the >= min_cluster_size REM threshold). Memories REM never
        touched fall back to the same tag-overlap grouping v1 used, and
        singletons that share no tags with anyone still get their own
        knowledge entry rather than being silently dropped.
        """
        groups: dict[str, list[Memory]] = defaultdict(list)
        ungrouped: list[Memory] = []
        for m in memories:
            tag = cluster_map.get(m.id)
            if tag:
                groups[f"rem-{tag}"].append(m)
            else:
                ungrouped.append(m)

        assigned: set[str] = set()
        for i, m1 in enumerate(ungrouped):
            if m1.id in assigned:
                continue
            tags1 = set(m1.tags or [])
            cluster = [m1]
            assigned.add(m1.id)
            for m2 in ungrouped[i + 1:]:
                if m2.id in assigned:
                    continue
                if tags1 & set(m2.tags or []):
                    cluster.append(m2)
                    assigned.add(m2.id)
            if len(cluster) >= 2:
                key = "adhoc-" + hashlib.sha1(",".join(sorted(x.id for x in cluster)).encode()).hexdigest()[:8]
                groups[key] = cluster
            else:
                groups[f"solo-{m1.id}"] = cluster

        return groups

    @staticmethod
    def _parse_source_ids(content: str) -> list[str]:
        import re

        match = re.search(r"来源记忆 \(\d+\): (.+)$", content or "", re.MULTILINE)
        if not match:
            return []
        return [x.strip() for x in match.group(1).split(",") if x.strip()]

    def _build_knowledge_content(self, members: list[Memory]) -> tuple[str, str, list[str], list[str]]:
        top = max(members, key=lambda m: m.importance or 0)
        title = self._title(top)
        tag_pool = [
            t
            for m in members
            for t in (m.tags or [])
            if not str(t).startswith(("promoted:", "dream-cluster:", "auto-knowledge"))
        ]
        common_tags = [t for t, _ in Counter(tag_pool).most_common(5)]
        ids = sorted(m.id for m in members)

        lines = [f"综合自 {len(members)} 条相关记忆的巩固摘要。", "", "**关键片段：**"]
        for m in members[:5]:
            snippet = (m.summary or m.content or "").strip().replace("\n", " ")[:120]
            if snippet:
                lines.append(f"- {snippet}")
        lines.append("")
        lines.append(f"来源记忆 ({len(ids)}): {', '.join(ids)}")
        content = "\n".join(lines)
        return title, content, common_tags, ids

    async def _upsert_knowledge(
        self,
        session,
        group_key: str,
        members: list[Memory],
        existing_by_cluster: dict[str, Memory],
        skip_notes: list[dict[str, Any]],
    ) -> tuple[str | None, dict[str, Any] | None]:
        cluster_tag = f"dream-cluster:{group_key}"
        title, content, common_tags, ids = self._build_knowledge_content(members)
        avg_importance = sum((m.importance or 0.5) for m in members) / len(members)
        new_importance = min(1.0, avg_importance * 1.1)

        existing = existing_by_cluster.get(cluster_tag)
        if existing is not None:
            prior_ids = self._parse_source_ids(existing.content)
            if prior_ids:
                overlap = len(set(prior_ids) & set(ids)) / len(prior_ids)
                if overlap < (1 - self._settings.dream_max_prior_loss_fraction):
                    skip_notes.append(
                        {"cluster": group_key, "prior_count": len(prior_ids), "new_count": len(ids)}
                    )
                    return existing.id, None
            existing.content = content
            existing.summary = title
            existing.importance = max(existing.importance or 0, new_importance)
            existing.tags = list(dict.fromkeys([*(existing.tags or []), cluster_tag, *common_tags, "auto-knowledge"]))
            return existing.id, {"id": existing.id, "title": title, "members": len(members)}

        mem = Memory(
            user_id="system",
            agent_id=members[0].agent_id or "default",
            type="knowledge",
            content=content,
            summary=title,
            importance=new_importance,
            tags=list(dict.fromkeys([cluster_tag, *common_tags, "auto-knowledge"])),
            source="dream",
        )
        session.add(mem)
        await session.flush()
        return mem.id, {"id": mem.id, "title": title, "members": len(members)}

    def _top_unmet(self, scored: list[tuple[Memory, dict]], promoted_ids: set[str]) -> list[dict[str, Any]]:
        rest = [(m, s) for m, s in scored if m.id not in promoted_ids]
        rest.sort(key=lambda pair: pair[1]["score"], reverse=True)
        return [{"id": m.id, "title": self._title(m), "score": round(s["score"], 4)} for m, s in rest[:2]]

    async def run_deep(self, *, force: bool = False) -> dict[str, Any]:
        """Score all in-window active memories, promote what clears every gate.

        Only phase that mutates Memory: importance bump + ``promoted:deep:hcc:
        <date>`` tag on promoted rows, plus create/update of ``type=knowledge``
        summary rows for the clusters they fall into. Publishes
        ``EventType.DREAM_FINISHED`` and writes both diary files exactly once
        per calendar day (idempotency guard below).
        """
        started = datetime.now(timezone.utc).replace(tzinfo=None)
        async with self._session_factory() as session:
            if not force:
                prior = await self._latest_run_today(session, "deep")
                if prior is not None:
                    logger.info("run_deep: already ran today (run_id=%s), skipping", prior.id)
                    return {**prior.stats, "skipped": True, "reason": "already_ran_today", "run_id": prior.id}

            run = DreamRun(phase="deep", started_at=started, stats={})
            session.add(run)
            await session.flush()

            cutoff = started - timedelta(days=self._settings.dream_max_age_days)
            result = await session.execute(
                select(Memory).where(Memory.status == MemoryStatus.ACTIVE).where(Memory.created_at >= cutoff)
            )
            candidates = list(result.scalars().all())

            signals_by_memory = await self._fetch_signals_by_memory(session, [m.id for m in candidates])
            cluster_map = await self._recent_cluster_tags(session)

            scored: list[tuple[Memory, dict[str, Any]]] = [
                (m, self._score_memory(m, signals_by_memory.get(m.id, []), started)) for m in candidates
            ]

            eligible = [
                (m, s)
                for m, s in scored
                if s["score"] >= self._settings.dream_min_score
                and (m.access_count or 0) >= self._settings.dream_min_access_count
                and s["age_days"] <= self._settings.dream_max_age_days
            ]
            eligible.sort(key=lambda pair: pair[1]["score"], reverse=True)
            promoted_pairs = eligible[: self._settings.dream_limit]

            date_tag = f"promoted:deep:hcc:{started.date().isoformat()}"
            promoted_records: list[dict[str, Any]] = []
            for m, s in promoted_pairs:
                tags = list(m.tags or [])
                if date_tag not in tags:
                    tags.append(date_tag)
                m.tags = tags
                m.importance = min(1.0, (m.importance or 0.0) + 0.1)
                promoted_records.append(
                    {"id": m.id, "title": self._title(m), "score": s["score"], "components": s}
                )

            knowledge_ids: list[str] = []
            knowledge_summaries: list[dict[str, Any]] = []
            knowledge_skip_notes: list[dict[str, Any]] = []
            if promoted_pairs:
                existing_result = await session.execute(
                    select(Memory).where(Memory.type == "knowledge").where(Memory.status == MemoryStatus.ACTIVE)
                )
                existing_by_cluster: dict[str, Memory] = {}
                for km in existing_result.scalars().all():
                    for t in km.tags or []:
                        if isinstance(t, str) and t.startswith("dream-cluster:"):
                            existing_by_cluster[t] = km

                groups = self._group_for_knowledge([m for m, _ in promoted_pairs], cluster_map)
                for group_key, members in groups.items():
                    kid, ksum = await self._upsert_knowledge(
                        session, group_key, members, existing_by_cluster, knowledge_skip_notes
                    )
                    if kid:
                        knowledge_ids.append(kid)
                    if ksum:
                        knowledge_summaries.append(ksum)

            promoted_ids = {m.id for m, _ in promoted_pairs}
            stats: dict[str, Any] = {
                "scanned": len(candidates),
                "eligible": len(eligible),
                "promoted": len(promoted_pairs),
                "promoted_memories": promoted_records,
                "knowledge_ids": knowledge_ids,
                "knowledge_skip_notes": knowledge_skip_notes,
                "top_unmet": self._top_unmet(scored, promoted_ids),
                "thresholds": {
                    "min_score": self._settings.dream_min_score,
                    "min_access_count": self._settings.dream_min_access_count,
                    "max_age_days": self._settings.dream_max_age_days,
                    "limit": self._settings.dream_limit,
                },
                "trigger_time": f"{self._settings.dream_deep_hour:02d}:{self._settings.dream_deep_minute:02d}",
            }

            # T2 (docs/emotion-design.md 2.5): nudge tonight's baseline anchor
            # from what got promoted, then record both the state that
            # produced tonight's diary and the cold daily snapshot (2.6).
            emotion_engine = get_emotion_engine()
            dream_items = [
                {"text": f"{m.summary or ''} {m.content or ''}", "importance": s["score"]}
                for m, s in promoted_pairs
            ]
            logger.info("DEBUG dream_items=%r", dream_items)
            emotion_adjustment = emotion_engine.apply_dream_adjustment(dream_items)
            logger.info("DEBUG emotion_adjustment=%r", emotion_adjustment)
            emotion_summary = emotion_engine.get_summary()
            stats["emotion_adjustment"] = emotion_adjustment

            try:
                snapshot_date = datetime.now().date()
                existing_snapshot = (
                    await session.execute(
                        select(EmotionSnapshot).where(EmotionSnapshot.snapshot_date == snapshot_date)
                    )
                ).scalars().first()
                snapshot_row = existing_snapshot or EmotionSnapshot(snapshot_date=snapshot_date)
                snapshot_row.state = emotion_summary["state"]
                snapshot_row.named_state = emotion_summary["named_state"]
                snapshot_row.dominant_trigger = emotion_adjustment.get("dominant_trigger")
                if existing_snapshot is None:
                    session.add(snapshot_row)
            except Exception:  # noqa: BLE001 - snapshot failure must not roll back promotions
                logger.exception("emotion snapshot write failed; deep promotions still committed")

            narrative_path: Path | None = None
            try:
                personality_summary = get_personality_engine().get_summary()
                rem_clusters = await self._todays_rem_clusters(session)
                diary_dir = Path(self._settings.dream_diary_dir).expanduser()
                # Diary heading uses the *local* calendar date (the diary is
                # for a human to read as "today") even though `started` and
                # every dream_signals/dream_runs idempotency check stay UTC,
                # matching the rest of the schema's UTC-naive convention. On
                # this host (system TZ AEST, UTC+10) the two dates disagree
                # for roughly ten hours of every local day, e.g. local
                # 09:xx == UTC of the *previous* calendar day — using
                # started.date() here mislabels the diary entry by a day.
                narrative_path = write_dream_diary(
                    date_=datetime.now().date(),
                    promoted=knowledge_summaries or promoted_records,
                    rem_clusters=rem_clusters,
                    emotion_summary=emotion_summary,
                    personality_summary=personality_summary,
                    stats=stats,
                    diary_dir=diary_dir,
                )
            except Exception:  # noqa: BLE001 - narrative failure must not roll back promotions
                logger.exception("dream narrative generation failed; deep promotions still committed")

            finished = datetime.now(timezone.utc).replace(tzinfo=None)
            run.finished_at = finished
            run.stats = stats
            run.narrative_path = str(narrative_path) if narrative_path else None
            await session.commit()
            logger.info(
                "run_deep complete: scanned=%s eligible=%s promoted=%s",
                stats["scanned"], stats["eligible"], stats["promoted"],
            )

        await emotion_engine.save_to_redis()

        try:
            bus = self._event_bus or get_event_bus()
            await bus.connect()
            await bus.publish_event(
                EventType.DREAM_FINISHED,
                {
                    "scanned": stats["scanned"],
                    "promoted": stats["promoted"],
                    "knowledge_ids": stats["knowledge_ids"],
                    "narrative_path": str(narrative_path) if narrative_path else None,
                },
                source="dream_engine",
            )
        except Exception:  # noqa: BLE001 - event publish is best-effort
            logger.exception("failed to publish DreamFinished event")

        return stats

    async def status(self) -> dict[str, Any]:
        """Last run per phase + configured thresholds, for GET /dream/status."""
        async with self._session_factory() as session:
            phases: dict[str, Any] = {}
            for phase in ("light", "rem", "deep"):
                result = await session.execute(
                    select(DreamRun).where(DreamRun.phase == phase).order_by(DreamRun.started_at.desc()).limit(1)
                )
                run = result.scalars().first()
                phases[phase] = (
                    None
                    if run is None
                    else {
                        "started_at": run.started_at.isoformat() if run.started_at else None,
                        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
                        "stats": run.stats,
                    }
                )
        return {
            "phases": phases,
            "config": {
                "auto_enabled": self._settings.dream_auto_enabled,
                "light_interval_hours": self._settings.dream_light_interval_hours,
                "rem_time": f"{self._settings.dream_rem_hour:02d}:{self._settings.dream_rem_minute:02d}",
                "deep_time": f"{self._settings.dream_deep_hour:02d}:{self._settings.dream_deep_minute:02d}",
                "min_score": self._settings.dream_min_score,
                "min_access_count": self._settings.dream_min_access_count,
                "max_age_days": self._settings.dream_max_age_days,
                "limit": self._settings.dream_limit,
                "diary_dir": str(Path(self._settings.dream_diary_dir).expanduser()),
            },
        }


# Singleton
_dream_engine: DreamEngine | None = None


def get_dream_engine() -> DreamEngine:
    global _dream_engine
    if _dream_engine is None:
        _dream_engine = DreamEngine()
    return _dream_engine

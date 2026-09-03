"""Assemble a unified context payload from memory + knowledge (+ emotion).

The :class:`ContextBuilder` is the single entry point the gateway's
``POST /context`` route (see later phases) will call. It fans a query out to the
:class:`MemoryManager` and :class:`KnowledgeManager`, optionally folds in an
emotional/personality state, and returns a structured dict that downstream
prompt assembly can consume directly.

Returned shape::

    {
        "context": "<human-readable, concatenated context text>",
        "sources": [
            {"provider": "memory",        "type": "memory",    "items": [...]},
            {"provider": "knowledge_qmd", "type": "knowledge", "items": [...]},
        ],
        "provider_metadata": {"memory": {...}, "knowledge_qmd": {...}},
        "emotion_state": {...} | None,
    }

Emotion/personality are optional and pluggable: no emotion backend exists yet,
so :meth:`build` accepts an injected ``emotion_provider`` callable and returns
``None`` for the state when unavailable, keeping the builder fully functional.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any
from collections.abc import Awaitable, Callable

from core.config import core_settings
from core.managers.knowledge_manager import KnowledgeManager
from core.managers.memory_manager import MemoryManager
from core.providers.base import SearchQuery

logger = logging.getLogger(__name__)

__all__ = ["ContextBuilder"]

# Memory types whose rows are raw tool-call logs, never fit for per-turn context
# injection. OpenClaw's tool_result_persist hook stores these at importance 0.3,
# but a non-trivial share were written/promoted to >=0.5 and so slip past the
# gateway's importance-based noise filter — this render-side type gate is the
# backstop that keeps them out of the injected "## Relevant Memories" block
# regardless of importance.
_NOISE_TYPES = {"tool_result"}

# Line prefixes that mark a content line as tool-log / retrieval-plumbing rather
# than a real memory headline. When a memory has no summary we fall back to its
# first usable content line (see _headline); these metadata lines are skipped so
# the injected context never shows raw tool output or RAG candidate scaffolding
# (confidence/evidence/recalls/status pointers) as a memory's title.
_JUNK_LINE_PREFIXES = (
    "[openclaw tool_result:",
    "confidence:",
    "evidence:",
    "recalls:",
    "status:",
    "source ",
    "source:",
)


def _clean_line(line: str) -> str:
    """Un-escape JSON artifacts, strip leaked structured-data noise, and
    collapse whitespace in a single line.

    Dream-extraction / compaction digests leak fragments like
    ``[emotion] {"type":"message","id":...} → session: <uuid>.jsonl`` into
    content (the JSON is frequently truncated with no closing brace). Cut the
    headline at the first such marker — everything after it is machine noise,
    never a memory title.
    """
    line = line.replace("\\n", " ").replace('\\"', '"').replace("\\\\", "\\")
    # Cut at the first inline JSON object (optionally preceded by a "[tag] ")
    # or a "→ session: …" pointer.
    line = re.split(r'\s*(?:\[[a-z_]+\]\s*)?\{"', line, maxsplit=1)[0]
    line = re.split(r"→\s*session:", line, maxsplit=1)[0]
    line = re.sub(r"\s+", " ", line).strip()
    # Drop dangling separators left behind after cutting the noise tail.
    line = line.rstrip(" -–—:;·|,")
    # If nothing but separators/punctuation survived, it isn't a headline.
    if not re.search(r"\w", line):
        return ""
    return line


def _headline(item: dict[str, Any]) -> str | None:
    """Derive a clean one-line headline for a memory, or ``None`` to drop it.

    Prefers a non-empty ``summary``; otherwise scans ``content`` for the first
    line that is neither empty, a bare JSON bracket, nor a known junk prefix.
    Returns ``None`` when nothing usable remains so the caller can skip the
    memory entirely rather than inject a garbage bullet.
    """
    if str(item.get("type") or "").lower() in _NOISE_TYPES:
        return None

    summary = (item.get("summary") or "").strip()
    if summary:
        cleaned = _clean_line(summary)
        if cleaned:
            return cleaned[:200]

    content = item.get("content") or ""
    for raw in content.splitlines():
        stripped = raw.strip()
        if not stripped or stripped in {"{", "}", "[", "]", "```"}:
            continue
        # Strip leading bullet/dash markers (possibly repeated, e.g. "- - ")
        # so prefix matching ignores RAG-nested bulleting.
        body = stripped.lstrip("-*• ")
        lowered = body.lower()
        # RAG "candidate digest" rows wrap the real text in a "Candidate:" line
        # followed by confidence/evidence/... metadata. The text after the colon
        # is the actual memory — unwrap it rather than skipping the whole line.
        if lowered.startswith("candidate:"):
            body = body.split(":", 1)[1]
            lowered = body.strip().lower()
        if not body.strip() or lowered.startswith(_JUNK_LINE_PREFIXES):
            continue
        cleaned = _clean_line(body)
        if cleaned:
            return cleaned[:200]

    return None

# An emotion provider is any (async) callable: user_id -> state dict | None.
EmotionProvider = Callable[[str], Awaitable[dict[str, Any] | None] | dict[str, Any] | None]


class ContextBuilder:
    """Compose memory, knowledge and emotion into one context payload.

    Parameters
    ----------
    memory_manager:
        Optional :class:`MemoryManager`; created with defaults if omitted.
    knowledge_manager:
        Optional :class:`KnowledgeManager`; created with defaults if omitted.
    emotion_provider:
        Optional callable returning an emotion/personality state for a user.
        Used only when ``include_emotion`` / ``include_personality`` is set.
    """

    def __init__(
        self,
        *,
        memory_manager: MemoryManager | None = None,
        knowledge_manager: KnowledgeManager | None = None,
        emotion_provider: EmotionProvider | None = None,
    ) -> None:
        self._memory = memory_manager or MemoryManager()
        self._knowledge = knowledge_manager or KnowledgeManager()
        self._emotion_provider = emotion_provider

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    async def build(
        self,
        query: str,
        user_id: str,
        *,
        include_emotion: bool = False,
        include_personality: bool = False,
        limit: int = 10,
    ) -> dict[str, Any]:
        """Build a unified context payload for ``query``/``user_id``.

        Parameters
        ----------
        query:
            Free-text query driving both memory and knowledge retrieval.
        user_id:
            Owner whose memories/emotion state are consulted.
        include_emotion:
            When ``True`` (or ``include_personality``), fold the emotion
            provider's state into the payload.
        include_personality:
            Treated as an alias/companion of ``include_emotion`` for callers
            that model personality as part of the emotional state.
        limit:
            Per-provider item cap.

        Returns
        -------
        dict
            The structured context payload (see module docstring).
        """
        # 1. Durable memory context (importance-ranked recent + keyword search).
        #    Over-fetch: junk/tool_result rows are filtered out at render time
        #    (see _headline), so ask for a wider pool than `limit` or those
        #    dropped rows would starve the injected block below its cap.
        #    Cross-scope (P1-3): 公子's memories are split across user_id scopes
        #    (michael + the Feishu/Hermes open_id), so search every scope in the
        #    identity group and merge, keeping the primary scope's results first.
        pool = max(limit * 3, limit)
        scopes = self._identity_scopes(user_id)
        if len(scopes) == 1:
            memory_result = await self._memory.search(
                SearchQuery(query=query, user_id=user_id, limit=pool)
            )
            memory_items = memory_result.items
        else:
            memory_items = await self._search_scopes(query, scopes, pool)

        # 2. Knowledge base context.
        knowledge_query = SearchQuery(query=query, limit=limit)
        knowledge_result = await self._knowledge.search(knowledge_query)

        # 3. Optional emotion / personality state.
        emotion_state: dict[str, Any] | None = None
        if (include_emotion or include_personality) and self._emotion_provider:
            emotion_state = await self._resolve_emotion(user_id)

        # 4. Assemble sources + metadata.
        sources = [
            {
                "provider": self._memory.provider.name,
                "type": "memory",
                "items": memory_items,
            },
            {
                "provider": knowledge_result.provider or "knowledge_qmd",
                "type": "knowledge",
                "items": knowledge_result.items,
            },
        ]

        provider_metadata = {
            self._memory.provider.name: _metadata_dict(
                await self._memory.provider.metadata()
            ),
            self._knowledge.provider.name: _metadata_dict(
                await self._knowledge.provider.metadata()
            ),
        }

        # 价值坐标(读路 join,§五):公子的 registry,读时现算——保送席 + 锚词加成。
        # 绝不改 memories;取不到就退化成纯相关性(不挡注入)。
        priorities: list[dict[str, Any]] = []
        try:
            from gateway.core.database import async_session
            from gateway.services.priority_service import PriorityService
            async with async_session() as psession:
                priorities = await PriorityService(psession).active_for_read(user_id=user_id)
        except Exception:
            logger.warning("priority fetch failed; injection falls back to relevance-only", exc_info=True)

        context_text = self._render_context(
            memory_items=memory_items,
            knowledge_items=knowledge_result.items,
            emotion_state=emotion_state,
            memory_limit=limit,
            priorities=priorities,
        )

        return {
            "context": context_text,
            "sources": sources,
            "provider_metadata": provider_metadata,
            "emotion_state": emotion_state,
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _identity_scopes(self, user_id: str) -> list[str]:
        """Return the user_id scopes to search for ``user_id``.

        Expands the primary id into its configured identity group (P1-3), always
        with the primary first and duplicates removed. Falls back to just
        ``[user_id]`` when no group is configured.
        """
        try:
            aliases = core_settings.identity_aliases.get(user_id, [])
        except Exception:
            aliases = []
        ordered = [user_id, *(a for a in aliases if a != user_id)]
        seen: set[str] = set()
        return [s for s in ordered if not (s in seen or seen.add(s))]

    async def _search_scopes(
        self, query: str, scopes: list[str], pool: int
    ) -> list[dict[str, Any]]:
        """Search each scope and merge, primary scope first, deduped by id.

        Each scope keeps its own relevance ranking; scopes are concatenated in
        order (primary owner's memories lead), so cross-scope recall is additive
        rather than reshuffling the primary results.
        """
        results = await asyncio.gather(
            *(
                self._memory.search(
                    SearchQuery(query=query, user_id=scope, limit=pool)
                )
                for scope in scopes
            ),
            return_exceptions=True,
        )
        merged: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for scope, result in zip(scopes, results):
            if isinstance(result, Exception):
                logger.warning("scope search failed for %s: %s", scope, result)
                continue
            for item in result.items:
                mid = item.get("id")
                if mid in seen_ids:
                    continue
                seen_ids.add(mid)
                merged.append(item)
        return merged

    async def _resolve_emotion(self, user_id: str) -> dict[str, Any] | None:
        """Invoke the (sync or async) emotion provider defensively."""
        try:
            result = self._emotion_provider(user_id)  # type: ignore[misc]
            if hasattr(result, "__await__"):
                result = await result  # type: ignore[assignment]
            return result  # type: ignore[return-value]
        except Exception:
            logger.warning("Emotion provider failed", exc_info=True)
            return None

    @staticmethod
    def _render_context(
        *,
        memory_items: list[dict[str, Any]],
        knowledge_items: list[dict[str, Any]],
        emotion_state: dict[str, Any] | None,
        memory_limit: int = 10,
        priorities: list[dict[str, Any]] | None = None,
    ) -> str:
        """Render the retrieved items into a readable context block.

        ``priorities`` 是 Priority Compass 读路 join 用的价值坐标(active、α>0),
        每条带 ``anchors`` / ``quadrant`` / ``alpha``。用法(§五 + Claude Code 会诊):
        - **保送席**:Q1 主题命中的记忆,在头部占至多 3 席,不参与相关性内卷、也
          绕开碎片配额;但**相关性地板 = 必须在检索池里**——池里没有就空着,绝不硬凑。
        - 其余按"蒸馏优先 + 碎片限 1/3"分层(见下)。
        """
        blocks: list[str] = []
        priorities = priorities or []

        headlines: list[str] = []
        seen: set[str] = set()

        def _dedup_key(h: str) -> str:
            # 同指纹/同 summary 前 80 字视作同一条:尾部微异的重复只留一条(公子 09-03)。
            return h.casefold().strip()[:80]

        def _add(item: dict[str, Any]) -> bool:
            """取干净 headline、去重后加入注入;成功返回 True。"""
            headline = _headline(item)
            if headline is None:
                return False
            key = _dedup_key(headline)
            if key in seen:
                return False
            seen.add(key)
            headlines.append(headline)
            return True

        # Q1(重要且紧急)主题锚词:保送席按这些命中拉人。
        q1_anchors = [
            str(a).casefold()
            for p in priorities if p.get("quadrant") == "Q1"
            for a in (p.get("anchors") or []) if str(a).strip()
        ]

        def _hits(item: dict[str, Any], anchors_lower: list[str]) -> bool:
            if not anchors_lower:
                return False
            hay = " ".join([
                str(item.get("summary") or ""),
                str(item.get("content") or ""),
                " ".join(item["tags"]) if isinstance(item.get("tags"), list) else str(item.get("tags") or ""),
            ]).casefold()
            return any(a in hay for a in anchors_lower)

        # 第 0 层 · 保送席:Q1 主题命中的记忆占头部至多 3 席(池里有才占,没有则空)。
        # 保送席绕开碎片配额——养伤这种 Q1 事,哪怕库里只有对话碎片,也该顶上来。
        reserved = 0
        RESERVED_CAP = 3
        if q1_anchors:
            for item in memory_items:
                if reserved >= RESERVED_CAP or len(headlines) >= memory_limit:
                    break
                if _hits(item, q1_anchors) and _add(item):
                    reserved += 1

        # 注入配额分层(公子 09-03 令:注入要干净、要分层):
        #   蒸馏记忆(有 summary 的 knowledge/decision 等)先占剩余预算;harvester 原始
        #   对话碎片降权限量——最多占 1/3,且排在蒸馏之后。碎片截半句、上下文全丢,能命中
        #   却没信息量,该在 memory_search 深挖时出场,不该霸占每轮系统注入位。
        distilled: list[dict[str, Any]] = []
        fragments_src: list[dict[str, Any]] = []
        for item in memory_items:
            if str(item.get("source") or "").startswith("harvester"):
                fragments_src.append(item)
            else:
                distilled.append(item)

        # 第一层:蒸馏记忆优先占位。
        for item in distilled:
            if len(headlines) >= memory_limit:
                break
            _add(item)

        # 第二层:harvester 碎片补位(仅当蒸馏填不满时兜底)。默认配额 0 ——
        # 收割来的原始对话是"深挖检索池",不是每轮系统注入的候选(公子 09-03:该在
        # memory_search 深挖时出场,不霸占每轮注入位)。保送席是唯一例外,不受此限。
        # 手感太薄可设 HCC_INJECT_FRAGMENT_CAP=3 放宽。
        fragment_cap = core_settings.inject_fragment_cap
        fragments = 0
        for item in fragments_src:
            if len(headlines) >= memory_limit or fragments >= fragment_cap:
                break
            if _add(item):
                fragments += 1

        if headlines:
            lines = ["## Relevant Memories"]
            lines.extend(f"- {h}" for h in headlines)
            blocks.append("\n".join(lines))

        if knowledge_items:
            lines = ["## Knowledge"]
            # 同一事实常被 qmd 切成多块/多篇重复记录 → 渲染层按指纹去重+限量(公子 09-03:BEES×5 病灶)
            kseen: set[str] = set()
            for item in knowledge_items:
                heading = (item.get("heading") or item.get("id") or "").strip()
                if not heading:
                    continue
                kkey = heading.casefold().strip()[:80]
                if kkey in kseen:
                    continue
                kseen.add(kkey)
                lines.append(f"- {heading}".rstrip())
                if len(kseen) >= 10:
                    break
            if len(lines) > 1:
                blocks.append("\n".join(lines))

        if emotion_state:
            mood = (
                emotion_state.get("mood")
                or emotion_state.get("named_state")
                or emotion_state.get("primary_emotion")
                or ""
            )
            intensity = emotion_state.get("intensity")
            hint = emotion_state.get("expression_hint") or ""
            head = f"- 心情(持续):{mood}" + (f"(强度 {intensity})" if intensity else "")
            lines = ["## Emotional State", head]
            if hint:  # 关键:把"该怎么把情绪演出来"的行为指导也给她,情绪才真驱动表达
                lines.append(f"- 表达倾向:{hint}")
            blocks.append("\n".join(lines).rstrip())

        return "\n\n".join(blocks)


def _metadata_dict(metadata: Any) -> dict[str, Any]:
    """Convert a :class:`ProviderMetadata` dataclass to a plain dict."""
    return {
        "name": metadata.name,
        "version": metadata.version,
        "capabilities": list(metadata.capabilities),
        "config": dict(metadata.config),
    }

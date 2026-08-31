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

import logging
import re
from typing import Any, Awaitable, Callable

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

        context_text = self._render_context(
            memory_items=memory_items,
            knowledge_items=knowledge_result.items,
            emotion_state=emotion_state,
            memory_limit=limit,
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
        except Exception:  # noqa: BLE001 — never let config shape break retrieval
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
        except Exception:  # noqa: BLE001
            logger.warning("Emotion provider failed", exc_info=True)
            return None

    @staticmethod
    def _render_context(
        *,
        memory_items: list[dict[str, Any]],
        knowledge_items: list[dict[str, Any]],
        emotion_state: dict[str, Any] | None,
        memory_limit: int = 10,
    ) -> str:
        """Render the retrieved items into a readable context block."""
        blocks: list[str] = []

        headlines: list[str] = []
        seen: set[str] = set()
        for item in memory_items:
            headline = _headline(item)
            if headline is None:
                continue
            # Drop near-identical bullets (case/space-insensitive) — the same
            # digest is often stored several times with trivial variation.
            key = headline.casefold().strip()
            if key in seen:
                continue
            seen.add(key)
            headlines.append(headline)
            if len(headlines) >= memory_limit:
                break

        if headlines:
            lines = ["## Relevant Memories"]
            lines.extend(f"- {h}" for h in headlines)
            blocks.append("\n".join(lines))

        if knowledge_items:
            lines = ["## Knowledge"]
            for item in knowledge_items:
                heading = (item.get("heading") or item.get("id") or "").strip()
                lines.append(f"- {heading}".rstrip())
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

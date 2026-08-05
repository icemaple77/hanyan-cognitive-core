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
from typing import Any, Awaitable, Callable

from core.managers.knowledge_manager import KnowledgeManager
from core.managers.memory_manager import MemoryManager
from core.providers.base import SearchQuery

logger = logging.getLogger(__name__)

__all__ = ["ContextBuilder"]

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
        memory_query = SearchQuery(query=query, user_id=user_id, limit=limit)
        memory_result = await self._memory.search(memory_query)

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
                "provider": memory_result.provider or "memory",
                "type": "memory",
                "items": memory_result.items,
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
            memory_items=memory_result.items,
            knowledge_items=knowledge_result.items,
            emotion_state=emotion_state,
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
    ) -> str:
        """Render the retrieved items into a readable context block."""
        blocks: list[str] = []

        if memory_items:
            lines = ["## Relevant Memories"]
            for item in memory_items:
                summary = (item.get("summary") or "").strip()
                content = (item.get("content") or "").strip()
                headline = summary or content.splitlines()[0] if content else ""
                lines.append(f"- {headline}".rstrip())
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
            blocks.append(f"## Emotional State\n- {mood}".rstrip())

        return "\n\n".join(blocks)


def _metadata_dict(metadata: Any) -> dict[str, Any]:
    """Convert a :class:`ProviderMetadata` dataclass to a plain dict."""
    return {
        "name": metadata.name,
        "version": metadata.version,
        "capabilities": list(metadata.capabilities),
        "config": dict(metadata.config),
    }

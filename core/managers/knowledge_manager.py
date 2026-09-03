"""High-level orchestration around the :class:`KnowledgeQMDProvider`.

The :class:`KnowledgeManager` owns a knowledge provider, exposes search over
the knowledge base, and — when Redis is enabled — publishes
:class:`~core.event_bus.EventType.KNOWLEDGE_MERGED` events when documents are
written/merged so downstream consumers can refresh caches or indexes.

Configuration
-------------
``HCC_EVENTS_ENABLED``
    Enable EventBus publishing (see :class:`ManagerSettings`).
``HCC_KNOWLEDGE_PROVIDER`` / ``HCC_QMD_DIR``
    Passed through to :class:`KnowledgeQMDProvider`.
"""

from __future__ import annotations

import logging
from typing import Any

from core.event_bus import EventBus, EventType
from core.managers.memory_manager import ManagerSettings
from core.providers.base import (
    SearchQuery,
    SearchResult,
    StoreData,
    StoreResult,
    UpdateData,
)
from core.providers.knowledge_qmd import KnowledgeQMDProvider

logger = logging.getLogger(__name__)

__all__ = ["KnowledgeManager"]


class KnowledgeManager:
    """Lifecycle + orchestration wrapper around a knowledge provider.

    Parameters
    ----------
    provider:
        Optional pre-built :class:`KnowledgeQMDProvider` (injectable for tests).
    settings:
        Optional :class:`ManagerSettings` (shared with the memory manager).
    event_bus:
        Optional :class:`EventBus`; created lazily when events are enabled.
    """

    def __init__(
        self,
        *,
        provider: KnowledgeQMDProvider | None = None,
        settings: ManagerSettings | None = None,
        event_bus: EventBus | None = None,
    ) -> None:
        self._settings = settings or ManagerSettings()
        self._provider = provider or KnowledgeQMDProvider()
        self._bus = event_bus
        self._bus_owned = event_bus is None

    @property
    def provider(self) -> KnowledgeQMDProvider:
        """Return the underlying knowledge provider."""
        return self._provider

    # ------------------------------------------------------------------
    # Lifecycle / events
    # ------------------------------------------------------------------
    async def _ensure_bus(self) -> EventBus | None:
        """Lazily connect the EventBus when events are enabled."""
        if not self._settings.events_enabled:
            return None
        if self._bus is None:
            self._bus = EventBus()
        await self._bus.connect()
        return self._bus

    async def aclose(self) -> None:
        """Release the EventBus connection if this manager owns it."""
        if self._bus is not None and self._bus_owned:
            await self._bus.close()
            self._bus = None

    async def _emit_merged(self, payload: dict[str, Any]) -> None:
        """Best-effort publish of a KNOWLEDGE_MERGED event."""
        try:
            bus = await self._ensure_bus()
            if bus is not None:
                await bus.publish_event(EventType.KNOWLEDGE_MERGED, payload)
        except Exception:
            logger.warning("Failed to publish KNOWLEDGE_MERGED event", exc_info=True)

    # ------------------------------------------------------------------
    # Read / write
    # ------------------------------------------------------------------
    async def search(self, query: SearchQuery) -> SearchResult:
        """Search across the knowledge base via the provider."""
        return await self._provider.search(query)

    async def store(self, data: StoreData) -> StoreResult:
        """Create/merge a knowledge document and emit KNOWLEDGE_MERGED."""
        result = await self._provider.store(data)
        if result.success:
            await self._emit_merged(
                {"id": result.id, "action": "store", "type": data.type}
            )
        return result

    async def update(self, data: UpdateData) -> StoreResult:
        """Update a knowledge document and emit KNOWLEDGE_MERGED."""
        result = await self._provider.update(data)
        if result.success:
            await self._emit_merged({"id": result.id, "action": "update"})
        return result

    async def delete(self, id: str) -> bool:
        """Delete a knowledge document and emit KNOWLEDGE_MERGED."""
        ok = await self._provider.delete(id)
        if ok:
            await self._emit_merged({"id": id, "action": "delete"})
        return ok

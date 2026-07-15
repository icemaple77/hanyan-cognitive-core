"""High-level orchestration around the durable :class:`MemoryProvider`.

The :class:`MemoryManager` owns a memory provider instance, adds convenience
operations used by the context builder (e.g. "give me the recent important
memories for this user"), and — when Redis is enabled — publishes
:class:`~core.event_bus.EventType.MEMORY_UPDATED` events on writes so other
components (knowledge merge, dream cycles, ...) can react.

Configuration
-------------
``HCC_EVENTS_ENABLED``
    When ``true`` the manager lazily connects an :class:`~core.event_bus.EventBus`
    and emits events on store/update/delete. Defaults to ``false`` so the
    manager works with no Redis dependency.
``HCC_MEMORY_PROVIDER``
    Passed through to :class:`MemoryProvider` (backend selector).
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from core.event_bus import EventBus, EventType
from core.providers.base import (
    SearchQuery,
    SearchResult,
    StoreData,
    StoreResult,
    UpdateData,
)
from core.providers.memory import MemoryProvider

logger = logging.getLogger(__name__)

__all__ = ["ManagerSettings", "MemoryManager"]


class ManagerSettings(BaseSettings):
    """Shared manager settings sourced from ``HCC_*`` env vars."""

    model_config = SettingsConfigDict(
        env_prefix="HCC_", env_file=".env", extra="ignore"
    )

    events_enabled: bool = Field(
        default=False,
        description="Publish domain events via Redis EventBus (HCC_EVENTS_ENABLED).",
    )


class MemoryManager:
    """Lifecycle + orchestration wrapper around a :class:`MemoryProvider`.

    Parameters
    ----------
    provider:
        Optional pre-built provider (injectable for tests).
    settings:
        Optional :class:`ManagerSettings`.
    event_bus:
        Optional pre-built :class:`EventBus`. When omitted and events are
        enabled, one is created lazily on first use.
    """

    def __init__(
        self,
        *,
        provider: MemoryProvider | None = None,
        settings: ManagerSettings | None = None,
        event_bus: EventBus | None = None,
    ) -> None:
        self._settings = settings or ManagerSettings()
        self._provider = provider or MemoryProvider()
        self._bus = event_bus
        self._bus_owned = event_bus is None

    @property
    def provider(self) -> MemoryProvider:
        """Return the underlying memory provider."""
        return self._provider

    # ------------------------------------------------------------------
    # Lifecycle
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

    async def _emit_updated(self, payload: dict[str, Any]) -> None:
        """Best-effort publish of a MEMORY_UPDATED event."""
        try:
            bus = await self._ensure_bus()
            if bus is not None:
                await bus.publish_event(EventType.MEMORY_UPDATED, payload)
        except Exception:  # noqa: BLE001
            logger.warning("Failed to publish MEMORY_UPDATED event", exc_info=True)

    # ------------------------------------------------------------------
    # Read operations
    # ------------------------------------------------------------------
    async def get_context(
        self, user_id: str, limit: int = 10
    ) -> list[dict[str, Any]]:
        """Return recent, important memories for ``user_id``.

        Fetches the most recent memories (newest first) and re-orders the page
        by ``importance`` so the most salient items lead the returned context.
        """
        result = await self._provider.search(
            SearchQuery(query="", user_id=user_id, limit=limit, offset=0)
        )
        items = sorted(
            result.items,
            key=lambda m: float(m.get("importance") or 0.0),
            reverse=True,
        )
        return items

    async def search(self, query: SearchQuery) -> SearchResult:
        """Delegate a search to the underlying provider."""
        return await self._provider.search(query)

    # ------------------------------------------------------------------
    # Write operations (emit events)
    # ------------------------------------------------------------------
    async def store(self, data: StoreData) -> StoreResult:
        """Store a memory and emit a MEMORY_UPDATED event on success."""
        result = await self._provider.store(data)
        if result.success:
            await self._emit_updated(
                {"id": result.id, "action": "store", "user_id": data.user_id}
            )
        return result

    async def update(self, data: UpdateData) -> StoreResult:
        """Update a memory and emit a MEMORY_UPDATED event on success."""
        result = await self._provider.update(data)
        if result.success:
            await self._emit_updated({"id": result.id, "action": "update"})
        return result

    async def delete(self, id: str) -> bool:
        """Soft-delete a memory and emit a MEMORY_UPDATED event on success."""
        ok = await self._provider.delete(id)
        if ok:
            await self._emit_updated({"id": id, "action": "delete"})
        return ok

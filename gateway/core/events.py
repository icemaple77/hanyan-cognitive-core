"""Shared EventBus singleton for the gateway process.

One process-wide :class:`~core.event_bus.EventBus` backs both the memory
CRUD routes (which publish) and the SSE stream endpoint (which subscribes),
so a store/update/delete is visible to any client on
``GET /api/v1/events/stream`` immediately — and, once ``HCC_REDIS_ENABLED``
is true, to other processes (Hermes/OpenClaw/Claude Code, or a future second
HCC instance) subscribed to the same Redis channels.
"""

from __future__ import annotations

import logging
from typing import Any

from core.event_bus import EventBus, EventType

logger = logging.getLogger(__name__)

_ACTION_TO_EVENT_TYPE: dict[str, EventType] = {
    "store": EventType.MEMORY_CREATED,
    "update": EventType.MEMORY_UPDATED,
    "delete": EventType.MEMORY_DELETED,
}

_bus: EventBus | None = None


def get_event_bus() -> EventBus:
    """Return the process-wide :class:`EventBus`, creating it on first use."""
    global _bus
    if _bus is None:
        _bus = EventBus()
    return _bus


async def publish_memory_event(action: str, memory_id: str, **extra: Any) -> None:
    """Best-effort publish of a memory store/update/delete event.

    Never raises — a Redis hiccup must not fail the memory API request that
    triggered it.
    """
    event_type = _ACTION_TO_EVENT_TYPE[action]
    try:
        bus = get_event_bus()
        await bus.connect()
        await bus.publish_event(event_type, {"memory_id": memory_id, "action": action, **extra})
    except Exception:  # noqa: BLE001
        logger.warning(
            "Failed to publish memory event action=%s memory_id=%s", action, memory_id, exc_info=True
        )

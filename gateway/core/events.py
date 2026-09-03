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
    except Exception:
        logger.warning(
            "Failed to publish memory event action=%s memory_id=%s", action, memory_id, exc_info=True
        )


async def publish_conflict_event(old_memory_id: str, new_memory_id: str, distance: float, **extra: Any) -> None:
    """Best-effort publish of a stale/conflict flag (体检报告 P1-3).

    Fired alongside the ``MemoryConflict`` audit row written by
    :meth:`gateway.services.MemoryService._flag_stale_duplicates` — the DB
    row is the durable record, this is the real-time notification (visible
    on ``GET /api/v1/events/stream`` same as store/update/delete). Never
    raises, same rationale as :func:`publish_memory_event`.
    """
    try:
        bus = get_event_bus()
        await bus.connect()
        await bus.publish_event(
            EventType.MEMORY_CONFLICT,
            {"old_memory_id": old_memory_id, "new_memory_id": new_memory_id, "distance": distance, **extra},
        )
    except Exception:
        logger.warning(
            "Failed to publish conflict event old=%s new=%s", old_memory_id, new_memory_id, exc_info=True
        )

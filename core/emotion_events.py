"""Event-driven emotion triggers (docs/emotion-design.md 2.1 T1/T3).

Subscribes the process-wide :class:`~core.event_bus.EventBus` to memory
lifecycle events so the emotion engine reacts to conversation/storage
activity without any component calling it directly:

* ``MEMORY_CREATED`` — T1: runs the (now importance-scaled) T3 keyword pass
  over the stored content, using the memory's own ``importance`` as the
  scaling signal in place of a live ``Orchestrator.evaluate()`` call (P5 in
  the design doc's staged rollout — Orchestrator wiring is a later pass).
* ``MEMORY_DELETED`` — a small fixed structural nudge (see
  ``core.emotion._STRUCTURAL_TRIGGERS``); deletion carries no text to run
  keyword matching against.

Dream's Deep-phase T2 hook lives in :meth:`core.dream.DreamEngine.run_deep`
directly rather than here, because it needs the full promoted ``Memory`` rows
(content/tags/score), which the ``DREAM_FINISHED`` event payload doesn't
carry — see that method's docstring.

Mirrors the subscription pattern already used by
``gateway/api/sync_routes.py``: connect once in the gateway lifespan, one
callback per event type, all failures logged and swallowed (a Redis/emotion
hiccup must never break the memory API request that triggered it).
"""

from __future__ import annotations

import logging

from core.emotion import get_emotion_engine
from core.event_bus import Event, EventType
from gateway.core.events import get_event_bus

logger = logging.getLogger(__name__)


async def _on_memory_created(event: Event) -> None:
    content = event.payload.get("content")
    if not content:
        return
    importance = event.payload.get("importance")
    try:
        await get_emotion_engine().update_and_persist(
            str(content), source="memory_created", importance=importance
        )
    except Exception:  # noqa: BLE001 - never let emotion updates break the event pipeline
        logger.exception("emotion update failed for memory_created event")


async def _on_memory_deleted(_event: Event) -> None:
    try:
        await get_emotion_engine().apply_structural_event_and_persist("memory_deleted")
    except Exception:  # noqa: BLE001
        logger.exception("emotion update failed for memory_deleted event")


async def subscribe_emotion_events() -> None:
    """Subscribe the emotion engine to memory CRUD events.

    Called once from the gateway lifespan on startup, alongside
    ``sync_routes.subscribe_sync_events()``.
    """
    bus = get_event_bus()
    await bus.connect()
    await bus.subscribe([EventType.MEMORY_CREATED], _on_memory_created)
    await bus.subscribe([EventType.MEMORY_DELETED], _on_memory_deleted)
    logger.info("emotion_events: subscribed to MEMORY_CREATED/MEMORY_DELETED")

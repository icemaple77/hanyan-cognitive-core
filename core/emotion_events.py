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

# 节流:同一秒窗口内的 MEMORY_CREATED 只触发一次情绪更新(2026-08-09 修复)。
# 背景:OpenClaw 插件 session_end 每轮对话都 store 记忆 → MEMORY_CREATED →
# update_and_persist 灌一次 soul 偏移;对话频率(秒级) >> decay 频率(小时级),
# 状态必然饱和到 1.0(named 恒为"依恋")。节流把写入压到最多 1 次/30s,
# 让 decay 有机会把状态拉回基线。30s 窗口对"情绪随对话渐进变化"足够细,
# 对"防饱和"足够粗——一次对话的连续几轮会被合并成一次情绪更新。
_EMOTION_UPDATE_MIN_INTERVAL = 30.0  # 秒
_last_emotion_update_at: float = 0.0


async def _on_memory_created(event: Event) -> None:
    global _last_emotion_update_at
    content = event.payload.get("content")
    if not content:
        return
    importance = event.payload.get("importance")
    try:
        import time as _time
        now = _time.monotonic()
        if now - _last_emotion_update_at < _EMOTION_UPDATE_MIN_INTERVAL:
            return  # 节流窗口内,跳过这次情绪更新(记忆仍正常存储,只是不灌情绪)
        _last_emotion_update_at = now
        await get_emotion_engine().update_and_persist(
            str(content), source="memory_created", importance=importance
        )
    except Exception:
        logger.exception("emotion update failed for memory_created event")


async def _on_memory_deleted(_event: Event) -> None:
    try:
        await get_emotion_engine().apply_structural_event_and_persist("memory_deleted")
    except Exception:
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

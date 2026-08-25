"""Event-driven local-model noise review (docs/local-noise-filter.md 四).

Subscribes the process-wide :class:`~core.event_bus.EventBus` to
``MEMORY_CREATED`` so low-trust memory writes (OpenClaw's ``tool_result``
auto-log hook, currently ~300 rows and the single largest noise source per
docs/local-noise-filter.md 零) get an async second opinion from
``core.local_filter`` without adding latency to ``/memory/store`` — the review
runs after the HTTP response has already gone back to the caller.

Mirrors the subscription pattern in ``core.emotion_events``: connect once in
the gateway lifespan, one callback, all failures logged and swallowed (an
Ollama hiccup must never surface as a memory-API error, and never touches the
row that triggered it beyond what the review itself decides).
"""

from __future__ import annotations

import logging

from core.config import core_settings
from core.event_bus import Event, EventType
from core.local_filter import DISCARDED_STATUS, NOISE_FILTER_TAG, evaluate
from gateway.core.events import get_event_bus

logger = logging.getLogger(__name__)

# Sources of memory writes that bypass evaluate()-gated storage and are known
# to be dominated by noise (docs/local-noise-filter.md 零/六). Deliberately
# narrow: the much larger openclaw_memory/openclaw_sync bulk-import bucket is
# out of scope for P0 and needs a separate confirmation before it's included.
LOW_TRUST_TYPES = {"tool_result"}
LOW_TRUST_SOURCES = {"openclaw_plugin"}

# Low-trust rows are LOGS, not curated knowledge. Even a keep-verdict must never
# score them high enough to surface in search: a tool_result that quotes real
# memories (a search dump) reads as "informative" to the evaluator and used to
# get boosted to ~0.85, leaking into retrieval. 2026-08-26: 870 such rows had
# been inflated this way (then purged); cap keep-verdicts below the search-drop
# threshold (0.5) so a log can never be re-inflated to a surfacing score again.
LOW_TRUST_IMPORTANCE_CAP = 0.4


def _is_low_trust(payload: dict) -> bool:
    return payload.get("type") in LOW_TRUST_TYPES or payload.get("source") in LOW_TRUST_SOURCES


async def _mark_discarded(memory_id: str) -> None:
    from gateway.core.database import async_session
    from gateway.models import Memory

    async with async_session() as session:
        memory = await session.get(Memory, memory_id)
        if memory is None:
            return
        memory.status = DISCARDED_STATUS
        memory.tags = list({*(memory.tags or []), NOISE_FILTER_TAG})
        await session.commit()


async def _update_importance(memory_id: str, importance: float) -> None:
    from gateway.core.database import async_session
    from gateway.models import Memory

    async with async_session() as session:
        memory = await session.get(Memory, memory_id)
        if memory is None:
            return
        memory.importance = importance
        memory.tags = list({*(memory.tags or []), NOISE_FILTER_TAG})
        await session.commit()


async def _on_memory_created(event: Event) -> None:
    if not core_settings.noise_filter_enabled:
        return
    payload = event.payload
    if not _is_low_trust(payload):
        return

    memory_id = payload.get("memory_id")
    content = payload.get("content")
    if not memory_id or not content:
        return

    try:
        decision = await evaluate(str(content), memory_source=payload.get("source", ""))
    except Exception:  # noqa: BLE001 - never let a filter bug break the event pipeline
        logger.exception("noise_filter: evaluate raised for memory_id=%s", memory_id)
        return

    try:
        if decision.keep:
            capped = min(decision.importance, LOW_TRUST_IMPORTANCE_CAP)  # 低信任日志封顶,永不浮出检索
            await _update_importance(memory_id, capped)
            logger.info(
                "noise_filter: kept memory_id=%s importance=%s->%.2f (capped from %.2f, verdict=%s)",
                memory_id, payload.get("importance"), capped, decision.importance, decision.source,
            )
        else:
            await _mark_discarded(memory_id)
            logger.info(
                "noise_filter: discarded memory_id=%s importance=%.2f (verdict=%s)",
                memory_id, decision.importance, decision.source,
            )
    except Exception:  # noqa: BLE001
        logger.exception("noise_filter: DB update failed for memory_id=%s", memory_id)


async def subscribe_noise_filter_events() -> None:
    """Subscribe the local noise filter to MEMORY_CREATED.

    Called once from the gateway lifespan on startup, alongside
    ``emotion_events.subscribe_emotion_events()``.
    """
    bus = get_event_bus()
    await bus.connect()
    await bus.subscribe([EventType.MEMORY_CREATED], _on_memory_created)
    logger.info(
        "noise_filter_events: subscribed to MEMORY_CREATED (enabled=%s, model=%s)",
        core_settings.noise_filter_enabled, core_settings.noise_filter_model,
    )

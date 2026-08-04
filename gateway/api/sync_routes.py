"""QMD sync trigger routes.

Exposes manual and status endpoints for the :class:`~core.sync_engine.SyncEngine`
(DB <-> QMD markdown bidirectional sync), plus a debounced event-driven trigger
so a memory store/update/delete is reflected in the Obsidian knowledge base
within seconds instead of waiting for the next periodic pass.

DESIGN: the debounced trigger subscribes directly to the process-wide
:class:`~core.event_bus.EventBus` (``gateway.core.events.get_event_bus``) for
``MEMORY_CREATED`` / ``MEMORY_UPDATED`` / ``MEMORY_DELETED``. This works
unmodified against both the in-memory and Redis backends and requires no
changes to ``memory_routes.py`` (which already calls ``publish_memory_event``
on every store/update/delete). Each event resets a 10s timer; the timer firing
runs a single ``sync_once()`` pass, so a burst of writes within the debounce
window collapses to one sync.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter

from core.config import core_settings
from core.event_bus import Event, EventType
from core.sync_engine import SyncEngine
from gateway.core.events import get_event_bus

logger = logging.getLogger(__name__)

router = APIRouter()

_DEBOUNCE_SECONDS = 10.0
_debounce_task: asyncio.Task[None] | None = None
_last_sync_at: str | None = None


async def _run_sync_once() -> dict[str, Any]:
    """Run a single :meth:`SyncEngine.sync_once` pass and record its timestamp."""
    global _last_sync_at
    stats = await SyncEngine().sync_once()
    _last_sync_at = datetime.now(timezone.utc).isoformat()
    return stats.as_dict()


async def _debounced_sync() -> None:
    """Sleep out the debounce window, then run one sync pass (errors logged only)."""
    try:
        await asyncio.sleep(_DEBOUNCE_SECONDS)
        logger.info("event-triggered sync starting after debounce window")
        await _run_sync_once()
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - never let a sync failure surface elsewhere
        logger.exception("event-triggered sync pass failed")


async def _on_memory_event(_event: Event) -> None:
    """Reset the debounce timer whenever a memory store/update/delete fires."""
    global _debounce_task
    if not core_settings.sync_auto_enabled:
        return
    if _debounce_task is not None and not _debounce_task.done():
        _debounce_task.cancel()
    _debounce_task = asyncio.create_task(_debounced_sync())


async def subscribe_sync_events() -> None:
    """Subscribe the debounced sync trigger to memory CRUD events.

    Called once from the gateway lifespan on startup. A no-op (but harmless)
    when ``HCC_SYNC_AUTO_ENABLED`` is false at call time, since the callback
    itself re-checks the flag on every event.
    """
    bus = get_event_bus()
    await bus.connect()
    await bus.subscribe(
        [EventType.MEMORY_CREATED, EventType.MEMORY_UPDATED, EventType.MEMORY_DELETED],
        _on_memory_event,
    )
    logger.info("sync_routes: subscribed to memory events (debounce=%ss)", _DEBOUNCE_SECONDS)


@router.post("/sync/qmd")
async def trigger_sync() -> dict[str, Any]:
    """Run one bidirectional QMD<->DB sync pass on demand."""
    try:
        return await _run_sync_once()
    except Exception as exc:  # noqa: BLE001 - report to the caller
        logger.exception("manual sync pass failed")
        from fastapi import HTTPException

        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/sync/status")
async def sync_status() -> dict[str, Any]:
    """Report the configured QMD directory, automation state and last sync time."""
    qmd_dir = Path(core_settings.qmd_dir).expanduser()
    readme = qmd_dir / "Knowledge" / "README.md"
    last_sync = _last_sync_at
    if last_sync is None and readme.exists():
        last_sync = datetime.fromtimestamp(
            readme.stat().st_mtime, tz=timezone.utc
        ).isoformat()
    return {
        "qmd_dir": str(qmd_dir),
        "last_sync_at": last_sync,
        "enabled": core_settings.sync_auto_enabled,
        "sync_interval": core_settings.sync_interval,
    }

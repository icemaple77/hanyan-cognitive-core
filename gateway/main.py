#!/usr/bin/env python3
"""Hanyan Cognitive Core — Memory Gateway API"""

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from core.config import core_settings
from core.dream import DreamEngine
from core.emotion import get_emotion_engine
from core.emotion_events import subscribe_emotion_events
from core.noise_filter_events import subscribe_noise_filter_events
from core.sync_engine import SyncEngine
from gateway.api import health, memory_routes, context_routes, graph_routes, emotion_routes, cognitive_routes, document_routes, events_routes, sync_routes, dream_routes, vault_routes, export_routes
from gateway.core.database import engine, Base
from gateway.core.events import get_event_bus

logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
logger = logging.getLogger(__name__)


def _seconds_until(hour: int, minute: int) -> float:
    """Seconds from now (local time) until the next occurrence of hour:minute."""
    now = datetime.now()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


async def _dream_light_loop() -> None:
    """Every HCC_DREAM_LIGHT_INTERVAL_HOURS, run the Light consolidation phase.

    One independent loop per phase (see docs/dreaming-design.md 2.6): this is
    the *only* automatic dreaming trigger inside the HCC process. There is no
    second setInterval/manual-tool path racing it — that dual-path race is
    what caused the "今夜无梦" x3 duplicate-write bug on the OpenClaw plugin
    side (AICore/Dreams/DREAMS-2026-08-04.md), and DreamEngine.run_deep/run_rem
    additionally self-guard against being re-entered the same calendar day.
    """
    interval = core_settings.dream_light_interval_hours * 3600
    while True:
        try:
            await asyncio.sleep(interval)
            if not core_settings.dream_auto_enabled:
                continue
            logger.info("dream light phase starting (interval=%ss)", interval)
            await DreamEngine().run_light()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - keep the loop alive
            logger.exception("dream light phase failed")


async def _dream_rem_loop() -> None:
    """Daily at HCC_DREAM_REM_HOUR:HCC_DREAM_REM_MINUTE, run the REM phase."""
    while True:
        try:
            await asyncio.sleep(_seconds_until(core_settings.dream_rem_hour, core_settings.dream_rem_minute))
            if not core_settings.dream_auto_enabled:
                continue
            logger.info("dream REM phase starting")
            await DreamEngine().run_rem()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - keep the loop alive
            logger.exception("dream REM phase failed")


async def _dream_deep_loop() -> None:
    """Daily at HCC_DREAM_DEEP_HOUR:HCC_DREAM_DEEP_MINUTE, run the Deep phase."""
    while True:
        try:
            await asyncio.sleep(_seconds_until(core_settings.dream_deep_hour, core_settings.dream_deep_minute))
            if not core_settings.dream_auto_enabled:
                continue
            logger.info("dream deep phase starting")
            await DreamEngine().run_deep()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - keep the loop alive
            logger.exception("dream deep phase failed")


async def _periodic_sync_loop() -> None:
    """Run SyncEngine.sync_once() every HCC_SYNC_INTERVAL seconds, forever.

    Exceptions are logged and swallowed so a transient DB/filesystem hiccup
    never brings down the gateway process.
    """
    interval = core_settings.sync_interval
    while True:
        try:
            await asyncio.sleep(interval)
            if not core_settings.sync_auto_enabled:
                continue
            logger.info("periodic sync starting (interval=%ss)", interval)
            await SyncEngine().sync_once()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - keep the loop alive
            logger.exception("periodic sync pass failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.create_all)
    bus = await get_event_bus().connect()
    logger.info("EventBus connected (backend=%s)", bus.backend)

    restored = await get_emotion_engine().load_from_redis()
    logger.info("emotion state %s", "restored from redis" if restored else "starting from defaults")
    await subscribe_emotion_events()
    await subscribe_noise_filter_events()

    sync_task: asyncio.Task | None = None
    if core_settings.sync_auto_enabled:
        await sync_routes.subscribe_sync_events()
        sync_task = asyncio.create_task(_periodic_sync_loop())

    dream_tasks: list[asyncio.Task] = []
    if core_settings.dream_auto_enabled:
        dream_tasks = [
            asyncio.create_task(_dream_light_loop()),
            asyncio.create_task(_dream_rem_loop()),
            asyncio.create_task(_dream_deep_loop()),
        ]
        logger.info(
            "dream loops started (light every %sh, rem %02d:%02d, deep %02d:%02d)",
            core_settings.dream_light_interval_hours,
            core_settings.dream_rem_hour, core_settings.dream_rem_minute,
            core_settings.dream_deep_hour, core_settings.dream_deep_minute,
        )

    yield
    # Shutdown
    if sync_task is not None:
        sync_task.cancel()
        try:
            await sync_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    for task in dream_tasks:
        task.cancel()
    for task in dream_tasks:
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    await get_event_bus().close()
    await engine.dispose()


app = FastAPI(
    title="Hanyan Cognitive Core",
    description="Memory Operating System for AI Agents",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Routes
app.include_router(health.router, prefix="/api/v1", tags=["health"])
app.include_router(memory_routes.router, prefix="/api/v1", tags=["memory"])
app.include_router(context_routes.router, prefix="/api/v1", tags=["context"])
app.include_router(graph_routes.router, prefix="/api/v1", tags=["graph"])
app.include_router(emotion_routes.router, prefix="/api/v1", tags=["emotion"])
app.include_router(cognitive_routes.router, prefix="/api/v1", tags=["cognitive"])
app.include_router(document_routes.router, prefix="/api/v1", tags=["document"])
app.include_router(events_routes.router, prefix="/api/v1", tags=["events"])
app.include_router(sync_routes.router, prefix="/api/v1", tags=["sync"])
app.include_router(dream_routes.router, prefix="/api/v1", tags=["dream"])
app.include_router(vault_routes.router, prefix="/api/v1", tags=["vault"])
app.include_router(export_routes.router, prefix="/api/v1", tags=["export"])

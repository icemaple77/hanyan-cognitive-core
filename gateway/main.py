#!/usr/bin/env python3
"""Hanyan Cognitive Core — Memory Gateway API"""

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

# 保留 load_dotenv():配置已统一到 core.config(pydantic Settings 自带 env_file),
# 但把 .env 灌进真实环境仍有用——独立脚本 / MCP 子进程 / 第三方库仍按 env 读。
load_dotenv()

from core.config import core_settings
from core.dream import DreamEngine
from core.emotion import get_emotion_engine
from core.emotion_events import subscribe_emotion_events
from core.noise_filter_events import subscribe_noise_filter_events
from core.sync_engine import SyncEngine
from gateway.api import health, memory_routes, context_routes, graph_routes, emotion_routes, cognitive_routes, document_routes, events_routes, sync_routes, dream_routes, vault_routes, export_routes, task_routes, priority_routes
from gateway.core.database import engine, Base
from gateway.core.events import get_event_bus
from gateway.core.vector_guard import check_vector_dims

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
        except Exception:
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
        except Exception:
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
        except Exception:
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
        except Exception:
            logger.exception("periodic sync pass failed")


async def _harvester_loop() -> None:
    """每 HCC_HARVEST_INTERVAL 秒收割各 runtime 会话文件的新对话入库(过 4b 初筛)。

    Agent 无感:纯读会话文件(openclaw/claude/…),不依赖任何插件钩子——这是公子
    最早的设计(docs/03「拉取全部对话」),把 08-26 漂成插件打包的记忆链路拉回来。
    见 core/session_harvester.py。异常吞掉保活。
    """
    from core.session_harvester import SessionHarvester

    interval = core_settings.harvest_interval
    harvester = SessionHarvester()
    logger.info("session harvester started (interval=%ss)", interval)
    while True:
        try:
            await asyncio.sleep(interval)
            await harvester.harvest_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("session harvest pass failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.create_all)
        # create_all 只建新表(priorities 走这)、不给已存在的表加列。tasks 表已存在,
        # 循环任务新增的 repeat 列必须显式补,否则 ORM 期待的列库里没有 → task_create 崩。
        await conn.execute(text("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS repeat VARCHAR(64)"))
        # 向量维度自检:换模型漏迁移会让相关表的语义检索静默降级(2026-08-29 事故,
        # documents 死了 5 天没人知道)。不一致时大声报错并挂到 /health,但不拒绝
        # 启动——HCC 不能挂,宁可响铃也不停机。见 gateway/core/vector_guard.py。
        await check_vector_dims(conn, core_settings.embedding_dim)
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

    harvest_task: asyncio.Task | None = None
    if core_settings.harvester_enabled:
        harvest_task = asyncio.create_task(_harvester_loop())

    yield
    # Shutdown
    if harvest_task is not None:
        harvest_task.cancel()
        try:
            await harvest_task
        except (asyncio.CancelledError, Exception):
            pass
    if sync_task is not None:
        sync_task.cancel()
        try:
            await sync_task
        except (asyncio.CancelledError, Exception):
            pass
    for task in dream_tasks:
        task.cancel()
    for task in dream_tasks:
        try:
            await task
        except (asyncio.CancelledError, Exception):
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
app.include_router(task_routes.router, prefix="/api/v1", tags=["tasks"])
app.include_router(priority_routes.router, prefix="/api/v1", tags=["priorities"])

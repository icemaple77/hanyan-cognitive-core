#!/usr/bin/env python3
"""Hanyan Cognitive Core — Memory Gateway API"""

import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from gateway.api import health, memory_routes, context_routes, graph_routes, emotion_routes, cognitive_routes, document_routes, events_routes
from gateway.core.database import engine, Base
from gateway.core.events import get_event_bus

logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.create_all)
    bus = await get_event_bus().connect()
    logger.info("EventBus connected (backend=%s)", bus.backend)
    yield
    # Shutdown
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

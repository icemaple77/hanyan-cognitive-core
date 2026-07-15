#!/usr/bin/env python3
"""Hanyan Cognitive Core — Memory Gateway API"""

from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from gateway.api import health, memory_routes, context_routes, graph_routes, emotion_routes
from gateway.core.database import engine, Base


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.create_all)
    yield
    # Shutdown
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

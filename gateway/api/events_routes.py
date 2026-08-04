"""Server-Sent Events endpoint for real-time memory change notifications.

Bridges the EventBus (see :mod:`gateway.core.events`) to HTTP clients —
Hermes/OpenClaw/Claude Code — via SSE, so a memory.created/updated/deleted
event published by *any* process (over Redis when HCC_REDIS_ENABLED=true;
in-process only otherwise) shows up here immediately.
"""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from core.event_bus import Event, EventBus, EventType

logger = logging.getLogger(__name__)

router = APIRouter()

_MEMORY_EVENT_TYPES = [
    EventType.MEMORY_CREATED,
    EventType.MEMORY_UPDATED,
    EventType.MEMORY_DELETED,
]

# Send a comment line at this interval so idle connections/proxies don't time out.
_KEEPALIVE_SECONDS = 15.0


def _format_sse(event: Event) -> str:
    data = {
        "action": event.payload.get("action"),
        "memory_id": event.payload.get("memory_id"),
        "timestamp": event.timestamp,
        "source": event.source,
    }
    return f"event: {event.event_type.value}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@router.get("/events/stream")
async def stream_events(request: Request) -> StreamingResponse:
    """Stream memory store/update/delete events as they happen (SSE)."""

    async def generator():
        bus = EventBus()
        await bus.connect()
        queue: asyncio.Queue[Event] = asyncio.Queue()

        async def on_event(event: Event) -> None:
            await queue.put(event)

        await bus.subscribe(_MEMORY_EVENT_TYPES, on_event)
        logger.info("SSE client connected (backend=%s)", bus.backend)
        try:
            yield ": connected\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=_KEEPALIVE_SECONDS)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                yield _format_sse(event)
        finally:
            await bus.close()
            logger.info("SSE client disconnected")

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )

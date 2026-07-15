"""Asynchronous event bus built on Redis Pub/Sub for HCC v2.

The bus lets loosely-coupled components react to knowledge-layer events
(memory creation, knowledge merges, emotional-state changes, dream cycles,
...) without direct calls. Events are typed dataclasses carrying metadata
(``timestamp``, ``source``, ``payload``) and are serialised to JSON on the
wire.

Example
-------
>>> bus = EventBus()
>>> await bus.connect()
>>> async def on_event(event: Event) -> None:
...     print(event.event_type, event.payload)
>>> await bus.subscribe([EventType.MEMORY_CREATED], on_event)
>>> await bus.publish_event(EventType.MEMORY_CREATED, {"id": "abc"})
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from types import TracebackType
from typing import Any, Awaitable, Callable

import redis.asyncio as aioredis

from core.config import CoreSettings, core_settings

logger = logging.getLogger(__name__)


class EventType(str, Enum):
    """Enumeration of knowledge-layer event types.

    The string values double as the Redis channel suffix for each event.
    """

    MEMORY_CREATED = "memory.created"
    MEMORY_UPDATED = "memory.updated"
    KNOWLEDGE_MERGED = "knowledge.merged"
    EMOTION_CHANGED = "emotion.changed"
    DREAM_FINISHED = "dream.finished"


def _utcnow_iso() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Event:
    """Base event carrying metadata and an arbitrary JSON payload.

    Attributes
    ----------
    event_type:
        The :class:`EventType` describing the event.
    payload:
        Arbitrary JSON-serialisable event data.
    source:
        Identifier of the component that emitted the event.
    timestamp:
        ISO-8601 UTC emission time.
    """

    event_type: EventType
    payload: dict[str, Any] = field(default_factory=dict)
    source: str = "hcc"
    timestamp: str = field(default_factory=_utcnow_iso)

    def to_json(self) -> str:
        """Serialise the event to a JSON string."""
        data = asdict(self)
        data["event_type"] = self.event_type.value
        return json.dumps(data, ensure_ascii=False, default=str)

    @classmethod
    def from_json(cls, raw: str) -> "Event":
        """Reconstruct an :class:`Event` (or subclass) from JSON.

        The concrete subclass is selected from :data:`_EVENT_CLASSES` based on
        the ``event_type`` field, falling back to the base :class:`Event`.
        """
        data = json.loads(raw)
        event_type = EventType(data["event_type"])
        klass = _EVENT_CLASSES.get(event_type, cls)
        return klass(
            event_type=event_type,
            payload=data.get("payload", {}),
            source=data.get("source", "hcc"),
            timestamp=data.get("timestamp", _utcnow_iso()),
        )


# --- Typed event subclasses --------------------------------------------------
# Each subclass fixes ``event_type`` so callers can construct semantically
# meaningful events, e.g. ``MemoryCreated(payload={...})``.


@dataclass
class MemoryCreated(Event):
    """Emitted when a new Memory is persisted."""

    event_type: EventType = EventType.MEMORY_CREATED


@dataclass
class MemoryUpdated(Event):
    """Emitted when an existing Memory is modified."""

    event_type: EventType = EventType.MEMORY_UPDATED


@dataclass
class KnowledgeMerged(Event):
    """Emitted when memories/knowledge fragments are merged."""

    event_type: EventType = EventType.KNOWLEDGE_MERGED


@dataclass
class EmotionChanged(Event):
    """Emitted when the emotional state changes."""

    event_type: EventType = EventType.EMOTION_CHANGED


@dataclass
class DreamFinished(Event):
    """Emitted when a background 'dream' consolidation cycle completes."""

    event_type: EventType = EventType.DREAM_FINISHED


_EVENT_CLASSES: dict[EventType, type[Event]] = {
    EventType.MEMORY_CREATED: MemoryCreated,
    EventType.MEMORY_UPDATED: MemoryUpdated,
    EventType.KNOWLEDGE_MERGED: KnowledgeMerged,
    EventType.EMOTION_CHANGED: EmotionChanged,
    EventType.DREAM_FINISHED: DreamFinished,
}


# Callbacks may be synchronous or async; both are supported.
EventCallback = Callable[[Event], Awaitable[None] | None]


class EventBus:
    """Redis Pub/Sub event bus with typed events and clean async lifecycle.

    Parameters
    ----------
    redis_url:
        Redis connection URL. Defaults to ``HCC_REDIS_URL``.
    settings:
        Optional :class:`CoreSettings` (used for channel prefix / source).
    """

    def __init__(
        self,
        redis_url: str | None = None,
        *,
        settings: CoreSettings | None = None,
    ) -> None:
        self._settings = settings or core_settings
        self._url = redis_url or self._settings.redis_url
        self._prefix = self._settings.event_channel_prefix
        self._client: aioredis.Redis | None = None
        self._tasks: list[asyncio.Task[None]] = []
        self._pubsubs: list[aioredis.client.PubSub] = []

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def connect(self) -> "EventBus":
        """Open the Redis connection (idempotent). Returns ``self``."""
        if self._client is None:
            self._client = aioredis.from_url(
                self._url, encoding="utf-8", decode_responses=True
            )
        return self

    async def close(self) -> None:
        """Cancel subscriptions and close all Redis connections."""
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._tasks.clear()

        for pubsub in self._pubsubs:
            try:
                await pubsub.aclose()
            except Exception:  # noqa: BLE001
                logger.debug("Error closing pubsub", exc_info=True)
        self._pubsubs.clear()

        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> "EventBus":
        return await self.connect()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    @property
    def client(self) -> aioredis.Redis:
        """Return the live Redis client, raising if not connected."""
        if self._client is None:
            raise RuntimeError(
                "EventBus is not connected; call connect() or use "
                "'async with EventBus()'."
            )
        return self._client

    # ------------------------------------------------------------------
    # Channel helpers
    # ------------------------------------------------------------------
    def _channel(self, event_type: EventType) -> str:
        """Return the fully-qualified Redis channel for an event type."""
        return f"{self._prefix}:{event_type.value}"

    # ------------------------------------------------------------------
    # Publish / subscribe
    # ------------------------------------------------------------------
    async def publish_event(
        self,
        event_type: EventType | str,
        data: dict[str, Any] | None = None,
        *,
        source: str | None = None,
    ) -> Event:
        """Publish an event and return the constructed :class:`Event`.

        Parameters
        ----------
        event_type:
            The :class:`EventType` (or its string value) to publish.
        data:
            The event payload (JSON-serialisable dict).
        source:
            Emitting component; defaults to ``HCC_EVENT_SOURCE``.
        """
        et = EventType(event_type) if not isinstance(event_type, EventType) else event_type
        klass = _EVENT_CLASSES.get(et, Event)
        event = klass(
            event_type=et,
            payload=data or {},
            source=source or self._settings.event_source,
        )
        await self.client.publish(self._channel(et), event.to_json())
        logger.debug("Published %s to %s", et.value, self._channel(et))
        return event

    async def subscribe(
        self,
        event_types: list[EventType] | EventType,
        callback: EventCallback,
    ) -> asyncio.Task[None]:
        """Subscribe ``callback`` to one or more event types.

        A background task is spawned to dispatch incoming messages. The task
        is tracked and cancelled automatically on :meth:`close`.

        Parameters
        ----------
        event_types:
            A single :class:`EventType` or a list of them.
        callback:
            A sync or async callable invoked with each decoded :class:`Event`.

        Returns
        -------
        asyncio.Task
            The listener task (also tracked internally for cleanup).
        """
        if isinstance(event_types, EventType):
            event_types = [event_types]

        pubsub = self.client.pubsub()
        channels = [self._channel(et) for et in event_types]
        await pubsub.subscribe(*channels)
        self._pubsubs.append(pubsub)

        task = asyncio.create_task(self._reader(pubsub, callback))
        self._tasks.append(task)
        return task

    async def _reader(
        self, pubsub: aioredis.client.PubSub, callback: EventCallback
    ) -> None:
        """Dispatch loop: decode messages and invoke ``callback``."""
        try:
            async for message in pubsub.listen():
                if message.get("type") != "message":
                    continue
                try:
                    event = Event.from_json(message["data"])
                except (json.JSONDecodeError, KeyError, ValueError):
                    logger.warning("Dropping malformed event", exc_info=True)
                    continue
                try:
                    result = callback(event)
                    if asyncio.iscoroutine(result):
                        await result
                except Exception:  # noqa: BLE001
                    logger.exception("Event callback raised for %s", event.event_type)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("Event reader loop terminated unexpectedly")

"""Asynchronous event bus for HCC v2 with an optional Redis backend.

The bus lets loosely-coupled components react to knowledge-layer events
(memory creation, knowledge merges, emotional-state changes, dream cycles,
...) without direct calls. Events are typed dataclasses carrying metadata
(``timestamp``, ``source``, ``payload``) and are serialised to JSON on the
wire.

Two interchangeable backends are provided behind a single public interface:

* **Redis Pub/Sub** — used when ``HCC_REDIS_ENABLED`` is true (and the
  ``redis`` package is importable). Suitable for multi-process deployments.
* **In-memory broker** — the default. A process-global broker built on
  :class:`asyncio.Queue` that requires no external services, so publishers and
  subscribers created anywhere in the same process see each other's events.

Both backends expose the identical :meth:`EventBus.publish_event` /
:meth:`EventBus.subscribe` API, so callers never need to know which is active.

Example
-------
>>> bus = EventBus()                     # in-memory by default
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
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from types import TracebackType
from typing import Any
from collections.abc import Awaitable, Callable

try:  # Redis is an optional dependency (HCC_REDIS_ENABLED=false by default).
    import redis.asyncio as aioredis
except ImportError:  # pragma: no cover - exercised only without redis installed
    aioredis = None  # type: ignore[assignment]

from core.config import CoreSettings, core_settings

logger = logging.getLogger(__name__)


class EventType(str, Enum):
    """Enumeration of knowledge-layer event types.

    The string values double as the Redis channel suffix for each event.
    """

    MEMORY_CREATED = "memory.created"
    MEMORY_UPDATED = "memory.updated"
    MEMORY_DELETED = "memory.deleted"
    MEMORY_CONFLICT = "memory.conflict"
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
class MemoryDeleted(Event):
    """Emitted when a Memory is deleted."""

    event_type: EventType = EventType.MEMORY_DELETED


@dataclass
class MemoryConflictDetected(Event):
    """Emitted when a write-path staleness check flags an older memory as
    likely superseded by a just-stored one (体检报告 P1-3)."""

    event_type: EventType = EventType.MEMORY_CONFLICT


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
    EventType.MEMORY_DELETED: MemoryDeleted,
    EventType.MEMORY_CONFLICT: MemoryConflictDetected,
    EventType.KNOWLEDGE_MERGED: KnowledgeMerged,
    EventType.EMOTION_CHANGED: EmotionChanged,
    EventType.DREAM_FINISHED: DreamFinished,
}


# Callbacks may be synchronous or async; both are supported.
EventCallback = Callable[[Event], Awaitable[None] | None]


class _InMemoryBroker:
    """Process-global, asyncio-based fan-out broker for the in-memory backend.

    A single shared instance (see :data:`_GLOBAL_BROKER`) lets :class:`EventBus`
    objects created independently across a process exchange events without any
    external service. Messages are the same JSON strings used on the Redis wire,
    so both backends behave identically from the caller's perspective.
    """

    def __init__(self) -> None:
        self._subscribers: dict[str, set[asyncio.Queue[str]]] = defaultdict(set)

    def register(self, channel: str, queue: asyncio.Queue[str]) -> None:
        """Attach ``queue`` so it receives messages published to ``channel``."""
        self._subscribers[channel].add(queue)

    def unregister(self, channel: str, queue: asyncio.Queue[str]) -> None:
        """Detach ``queue`` from ``channel`` (no-op if already removed)."""
        self._subscribers.get(channel, set()).discard(queue)

    async def publish(self, channel: str, message: str) -> None:
        """Fan ``message`` out to every queue subscribed to ``channel``."""
        for queue in list(self._subscribers.get(channel, ())):
            queue.put_nowait(message)


# Shared, process-wide broker backing the in-memory EventBus backend.
_GLOBAL_BROKER = _InMemoryBroker()


class EventBus:
    """Typed event bus with a Redis or in-memory backend and async lifecycle.

    The backend is chosen at construction time from ``HCC_REDIS_ENABLED``:
    when true (and the ``redis`` package is importable) Redis Pub/Sub is used;
    otherwise a process-global in-memory broker is used. The public API is
    identical for both.

    Parameters
    ----------
    redis_url:
        Redis connection URL. Defaults to ``HCC_REDIS_URL``.
    settings:
        Optional :class:`CoreSettings` (channel prefix, source, redis switch).
    use_redis:
        Explicit backend override. When ``None`` (default) the backend is
        derived from settings + availability of the ``redis`` package.
    """

    def __init__(
        self,
        redis_url: str | None = None,
        *,
        settings: CoreSettings | None = None,
        use_redis: bool | None = None,
    ) -> None:
        self._settings = settings or core_settings
        self._url = redis_url or self._settings.redis_url
        self._prefix = self._settings.event_channel_prefix
        self._tasks: list[asyncio.Task[None]] = []

        if use_redis is None:
            use_redis = bool(self._settings.redis_enabled)
        if use_redis and aioredis is None:
            logger.warning(
                "HCC_REDIS_ENABLED is true but the 'redis' package is not "
                "installed; falling back to the in-memory EventBus backend."
            )
            use_redis = False
        self._use_redis = use_redis

        # Redis backend state.
        self._client: "aioredis.Redis | None" = None
        self._pubsubs: list[Any] = []
        # In-memory backend state.
        self._broker = _GLOBAL_BROKER
        self._queues: list[tuple[str, asyncio.Queue[str]]] = []
        self._connected = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    @property
    def backend(self) -> str:
        """Return the active backend name: ``"redis"`` or ``"memory"``."""
        return "redis" if self._use_redis else "memory"

    async def connect(self) -> "EventBus":
        """Open the backend connection (idempotent). Returns ``self``."""
        if self._use_redis and self._client is None:
            self._client = aioredis.from_url(
                self._url, encoding="utf-8", decode_responses=True
            )
        self._connected = True
        return self

    async def close(self) -> None:
        """Cancel subscriptions and release all backend resources."""
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks.clear()

        # Redis cleanup.
        for pubsub in self._pubsubs:
            try:
                await pubsub.aclose()
            except Exception:
                logger.debug("Error closing pubsub", exc_info=True)
        self._pubsubs.clear()
        if self._client is not None:
            await self._client.aclose()
            self._client = None

        # In-memory cleanup.
        for channel, queue in self._queues:
            self._broker.unregister(channel, queue)
        self._queues.clear()
        self._connected = False

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
    def client(self) -> "aioredis.Redis":
        """Return the live Redis client, raising if unavailable.

        Raises
        ------
        RuntimeError
            If the in-memory backend is active or the bus is not connected.
        """
        if not self._use_redis:
            raise RuntimeError(
                "EventBus is using the in-memory backend; no Redis client is "
                "available. Set HCC_REDIS_ENABLED=true to use Redis."
            )
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
        """Return the fully-qualified channel name for an event type."""
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
        et = (
            EventType(event_type)
            if not isinstance(event_type, EventType)
            else event_type
        )
        klass = _EVENT_CLASSES.get(et, Event)
        event = klass(
            event_type=et,
            payload=data or {},
            source=source or self._settings.event_source,
        )
        channel = self._channel(et)
        if self._use_redis:
            await self.client.publish(channel, event.to_json())
        else:
            if not self._connected:
                await self.connect()
            await self._broker.publish(channel, event.to_json())
        logger.debug("Published %s to %s (%s)", et.value, channel, self.backend)
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
        channels = [self._channel(et) for et in event_types]

        if self._use_redis:
            pubsub = self.client.pubsub()
            await pubsub.subscribe(*channels)
            self._pubsubs.append(pubsub)
            task = asyncio.create_task(self._redis_reader(pubsub, callback))
        else:
            if not self._connected:
                await self.connect()
            queue: asyncio.Queue[str] = asyncio.Queue()
            for channel in channels:
                self._broker.register(channel, queue)
                self._queues.append((channel, queue))
            task = asyncio.create_task(self._memory_reader(queue, callback))

        self._tasks.append(task)
        return task

    # ------------------------------------------------------------------
    # Dispatch loops
    # ------------------------------------------------------------------
    @staticmethod
    async def _dispatch(callback: EventCallback, raw: str) -> None:
        """Decode ``raw`` JSON into an :class:`Event` and invoke ``callback``."""
        try:
            event = Event.from_json(raw)
        except (json.JSONDecodeError, KeyError, ValueError):
            logger.warning("Dropping malformed event", exc_info=True)
            return
        try:
            result = callback(event)
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            logger.exception("Event callback raised for %s", event.event_type)

    async def _redis_reader(self, pubsub: Any, callback: EventCallback) -> None:
        """Redis dispatch loop: decode messages and invoke ``callback``."""
        try:
            async for message in pubsub.listen():
                if message.get("type") != "message":
                    continue
                await self._dispatch(callback, message["data"])
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Event reader loop terminated unexpectedly")

    async def _memory_reader(
        self, queue: asyncio.Queue[str], callback: EventCallback
    ) -> None:
        """In-memory dispatch loop: drain ``queue`` and invoke ``callback``."""
        try:
            while True:
                raw = await queue.get()
                await self._dispatch(callback, raw)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Event reader loop terminated unexpectedly")

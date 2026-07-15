"""Redis-backed working memory manager for HCC v2.

Working memory holds short-lived, TTL-bound state (chat context, task
progress, cached prompts and embeddings) that complements the durable
PostgreSQL long-term memory. Values are JSON-serialised transparently so
callers can store plain Python objects.

Example
-------
>>> async with RedisManager() as rm:
...     await rm.set_working("chat:42", {"turn": 1}, category="chat")
...     data = await rm.get_working("chat:42")
"""

from __future__ import annotations

import json
from types import TracebackType
from typing import Any

import redis.asyncio as aioredis

from core.config import CoreSettings, core_settings

# Category -> default TTL (seconds). Mirrors ``CoreSettings`` defaults and is
# exposed as a class constant for callers that want to introspect the policy.
DEFAULT_TTLS: dict[str, int] = {
    "chat": 1800,
    "task": 3600,
    "prompt": 3600,
    "embedding": 604800,
}


class RedisManager:
    """Async manager for TTL-based working memory backed by Redis.

    The manager can be used as an async context manager (which opens and
    closes the connection automatically) or driven manually via
    :meth:`connect` and :meth:`close`.

    Parameters
    ----------
    redis_url:
        Connection URL. Defaults to ``HCC_REDIS_URL`` via :class:`CoreSettings`.
    settings:
        Optional pre-built :class:`CoreSettings` instance (used for TTL lookup).
    """

    DEFAULT_TTLS = DEFAULT_TTLS

    def __init__(
        self,
        redis_url: str | None = None,
        *,
        settings: CoreSettings | None = None,
    ) -> None:
        self._settings = settings or core_settings
        self._url = redis_url or self._settings.redis_url
        self._client: aioredis.Redis | None = None

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------
    async def connect(self) -> "RedisManager":
        """Open the Redis connection (idempotent). Returns ``self``."""
        if self._client is None:
            self._client = aioredis.from_url(
                self._url,
                encoding="utf-8",
                decode_responses=True,
            )
        return self

    async def close(self) -> None:
        """Close the Redis connection and release the pool."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> "RedisManager":
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
                "RedisManager is not connected; call connect() or use "
                "'async with RedisManager()'."
            )
        return self._client

    # ------------------------------------------------------------------
    # Working-memory operations
    # ------------------------------------------------------------------
    def _resolve_ttl(self, ttl: int | None, category: str | None) -> int:
        """Resolve the effective TTL from an explicit value or a category."""
        if ttl is not None:
            return ttl
        if category is not None:
            return self._settings.ttl_for(category)
        return self._settings.ttl_for("chat")

    async def set_working(
        self,
        key: str,
        value: Any,
        ttl: int | None = None,
        *,
        category: str | None = None,
    ) -> None:
        """Store ``value`` under ``key`` with a TTL.

        Parameters
        ----------
        key:
            Redis key.
        value:
            Any JSON-serialisable object; stored as a JSON string.
        ttl:
            Explicit time-to-live in seconds. Takes precedence over ``category``.
        category:
            One of ``chat``/``task``/``prompt``/``embedding``; selects the
            default TTL when ``ttl`` is not given. Defaults to ``chat``.
        """
        payload = json.dumps(value, ensure_ascii=False, default=str)
        await self.client.set(key, payload, ex=self._resolve_ttl(ttl, category))

    async def get_working(self, key: str) -> Any | None:
        """Return the deserialised value for ``key`` or ``None`` if absent."""
        raw = await self.client.get(key)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            # Fall back to the raw string if it was not stored as JSON.
            return raw

    async def delete(self, key: str) -> bool:
        """Delete ``key``. Returns ``True`` if a key was removed."""
        return bool(await self.client.delete(key))

    async def exists(self, key: str) -> bool:
        """Return ``True`` if ``key`` currently exists."""
        return bool(await self.client.exists(key))

    async def ttl(self, key: str) -> int:
        """Return the remaining TTL (seconds) for ``key``.

        Returns ``-2`` if the key does not exist and ``-1`` if it has no
        expiry, mirroring the semantics of the Redis ``TTL`` command.
        """
        return int(await self.client.ttl(key))

    async def ping(self) -> bool:
        """Return ``True`` if the Redis server responds to PING."""
        return bool(await self.client.ping())

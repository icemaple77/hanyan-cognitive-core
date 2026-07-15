"""PostgreSQL-backed :class:`~core.providers.base.Provider` for durable memories.

This provider wraps the existing gateway persistence layer
(:mod:`gateway.core.database` + :class:`gateway.models.Memory`) behind the
neutral Provider SDK so the rest of HCC v2.1 can read/write long-term memory
without importing SQLAlchemy or the ORM model directly.

Session handling uses the gateway's ``async_session`` sessionmaker; every
public method opens a short-lived ``async with`` scope, commits on success and
rolls back on error.

Configuration
-------------
``HCC_MEMORY_PROVIDER``
    Backend selector. Only ``postgresql`` (the default) is currently supported;
    the value is surfaced in :meth:`metadata` so callers can introspect it.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import String, cast, func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from core.providers.base import (
    HealthStatus,
    Provider,
    ProviderMetadata,
    SearchQuery,
    SearchResult,
    StoreData,
    StoreResult,
    UpdateData,
)
from gateway.core.database import async_session
from gateway.core.embeddings import embed_text
from gateway.models import Memory

logger = logging.getLogger(__name__)

__all__ = ["MemoryProviderSettings", "MemoryProvider"]


class MemoryProviderSettings(BaseSettings):
    """Settings for the memory provider, sourced from ``HCC_*`` env vars."""

    model_config = SettingsConfigDict(
        env_prefix="HCC_", env_file=".env", extra="ignore"
    )

    memory_provider: str = Field(
        default="postgresql",
        description="Durable-memory backend selector (HCC_MEMORY_PROVIDER).",
    )


def _memory_to_dict(memory: Memory) -> dict[str, Any]:
    """Serialise a :class:`Memory` ORM row to a JSON-safe dict."""
    return {
        "id": memory.id,
        "user_id": memory.user_id,
        "type": memory.type,
        "content": memory.content,
        "summary": memory.summary,
        "importance": memory.importance,
        "tags": list(memory.tags or []),
        "source": memory.source,
        "status": memory.status,
        "created_at": memory.created_at.isoformat() if memory.created_at else None,
        "updated_at": memory.updated_at.isoformat() if memory.updated_at else None,
    }


class MemoryProvider(Provider):
    """Provider exposing PostgreSQL long-term memory via the Provider SDK.

    Parameters
    ----------
    settings:
        Optional :class:`MemoryProviderSettings`; defaults to env-derived.
    session_factory:
        Optional async sessionmaker (injectable for tests). Defaults to the
        gateway's shared ``async_session``.
    """

    name = "memory"
    version = "0.1.0"

    def __init__(
        self,
        *,
        settings: MemoryProviderSettings | None = None,
        session_factory: async_sessionmaker | None = None,
    ) -> None:
        self._settings = settings or MemoryProviderSettings()
        self._session_factory = session_factory or async_session

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------
    async def search(self, query: SearchQuery) -> SearchResult:
        """Keyword/tag search over active memories, newest first.

        The free-text ``query`` is matched (case-insensitively) against the
        memory ``content``, ``summary`` and serialised ``tags``. Optional
        ``user_id`` and ``type`` narrow the result set. Only ``active``
        memories are returned.
        """
        async with self._session_factory() as session:
            stmt = select(Memory).where(Memory.status == "active")
            count_stmt = select(func.count(Memory.id)).where(
                Memory.status == "active"
            )

            if query.user_id:
                stmt = stmt.where(Memory.user_id == query.user_id)
                count_stmt = count_stmt.where(Memory.user_id == query.user_id)
            if query.type:
                stmt = stmt.where(Memory.type == query.type)
                count_stmt = count_stmt.where(Memory.type == query.type)
            if query.query:
                like = f"%{query.query}%"
                # Keyword + tag filter: match content, summary or any tag
                # (tags are JSON, cast to text so ilike can scan them).
                predicate = (
                    Memory.content.ilike(like)
                    | Memory.summary.ilike(like)
                    | cast(Memory.tags, String).ilike(like)
                )
                stmt = stmt.where(predicate)
                count_stmt = count_stmt.where(predicate)

            stmt = (
                stmt.order_by(Memory.created_at.desc())
                .offset(query.offset)
                .limit(query.limit)
            )

            total = (await session.execute(count_stmt)).scalar() or 0
            rows = (await session.execute(stmt)).scalars().all()

        return SearchResult(
            items=[_memory_to_dict(m) for m in rows],
            total=int(total),
            provider=self.name,
        )

    # ------------------------------------------------------------------
    # Store
    # ------------------------------------------------------------------
    async def store(self, data: StoreData) -> StoreResult:
        """Create a new :class:`Memory` row (with a computed embedding)."""
        memory = Memory(
            user_id=data.user_id,
            type=data.type,
            content=data.content,
            summary=data.summary,
            importance=data.importance,
            tags=list(data.tags or []),
            source=data.source,
            status="active",
            embedding=embed_text(data.content),
        )
        async with self._session_factory() as session:
            session.add(memory)
            try:
                await session.flush()
                new_id = memory.id
                await session.commit()
            except Exception:
                await session.rollback()
                logger.exception("MemoryProvider.store failed")
                raise
        return StoreResult(id=new_id, success=True, provider=self.name)

    # ------------------------------------------------------------------
    # Update
    # ------------------------------------------------------------------
    async def update(self, data: UpdateData) -> StoreResult:
        """Apply a partial update to an existing memory.

        Only the non-``None`` fields on ``data`` are written. When ``content``
        changes, the stored embedding is recomputed.
        """
        async with self._session_factory() as session:
            memory = await session.get(Memory, data.id)
            if memory is None:
                return StoreResult(id=data.id, success=False, provider=self.name)

            if data.content is not None:
                memory.content = data.content
                memory.embedding = embed_text(data.content)
            if data.summary is not None:
                memory.summary = data.summary
            if data.importance is not None:
                memory.importance = data.importance
            if data.tags is not None:
                memory.tags = list(data.tags)
            if data.status is not None:
                memory.status = data.status

            try:
                await session.flush()
                await session.commit()
            except Exception:
                await session.rollback()
                logger.exception("MemoryProvider.update failed for %s", data.id)
                raise
        return StoreResult(id=data.id, success=True, provider=self.name)

    # ------------------------------------------------------------------
    # Delete (soft)
    # ------------------------------------------------------------------
    async def delete(self, id: str) -> bool:
        """Soft-delete a memory by setting ``status = "archived"``."""
        async with self._session_factory() as session:
            memory = await session.get(Memory, id)
            if memory is None:
                return False
            memory.status = "archived"
            try:
                await session.flush()
                await session.commit()
            except Exception:
                await session.rollback()
                logger.exception("MemoryProvider.delete failed for %s", id)
                raise
        return True

    # ------------------------------------------------------------------
    # Health & metadata
    # ------------------------------------------------------------------
    async def health(self) -> HealthStatus:
        """Ping the database with ``SELECT 1`` and measure round-trip latency."""
        start = time.perf_counter()
        healthy = True
        try:
            async with self._session_factory() as session:
                await session.execute(text("SELECT 1"))
        except Exception:  # noqa: BLE001
            logger.exception("MemoryProvider health check failed")
            healthy = False
        latency_ms = (time.perf_counter() - start) * 1000.0
        return HealthStatus(
            healthy=healthy,
            version=self.version,
            provider_name=self.name,
            latency_ms=round(latency_ms, 3),
        )

    async def metadata(self) -> ProviderMetadata:
        """Return static provider metadata (capabilities + config)."""
        return ProviderMetadata(
            name=self.name,
            version=self.version,
            capabilities=["search", "store", "update", "delete", "embedding"],
            config={"backend": self._settings.memory_provider},
        )

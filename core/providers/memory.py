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

import asyncio
import logging
import time
from typing import Any

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import func, select, text
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
from gateway.core.embeddings import embed_text, memory_embedding_text
from gateway.models import Memory, MemoryStatus
from gateway.services import MemoryService

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
        "agent_id": memory.agent_id,
        "type": memory.type,
        "content": memory.content,
        "summary": memory.summary,
        "importance": memory.importance,
        "tags": list(memory.tags or []),
        "source": memory.source,
        "status": memory.status,
        "access_count": memory.access_count,  # P2-7: was missing here while the
        # MCP _serialize path exposed it — two read paths returned different field
        # sets for the same row. Aligned so SDK callers see access_count too.
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
        """Hybrid (BM25 + vector) search over active memories, or a plain
        recency listing when ``query.query`` is empty.

        A non-empty ``query.query`` is delegated to
        :meth:`~gateway.services.MemoryService.hybrid_search`, which embeds
        the text server-side and fuses full-text (jieba-segmented BM25) and
        pgvector cosine-similarity branches with Reciprocal Rank Fusion. This
        is what lets a long free-text sentence — not just an exact
        substring — surface semantically related memories; a plain ``ilike``
        scan of the whole query string against ``content``/``summary``
        virtually never matches once the query is more than a couple of
        words. Optional ``user_id`` and ``type`` narrow the result set in
        both branches.

        An empty ``query.query`` keeps the previous newest-first listing
        (used by :meth:`MemoryManager.get_context`), since there is no
        keyword/vector signal to rank against.
        """
        async with self._session_factory() as session:
            if query.query:
                fused = await MemoryService(session).hybrid_search(
                    query=query.query,
                    limit=query.limit,
                    user_id=query.user_id,
                    agent_id=query.agent_id,
                    type=query.type,
                )
                items = [_memory_to_dict(item["memory"]) for item in fused]
                return SearchResult(
                    items=items, total=len(items), provider=self.name
                )

            stmt = select(Memory).where(Memory.status == MemoryStatus.ACTIVE)
            count_stmt = select(func.count(Memory.id)).where(
                Memory.status == MemoryStatus.ACTIVE
            )

            if query.user_id:
                stmt = stmt.where(Memory.user_id == query.user_id)
                count_stmt = count_stmt.where(Memory.user_id == query.user_id)
            if query.agent_id:
                stmt = stmt.where(Memory.agent_id == query.agent_id)
                count_stmt = count_stmt.where(Memory.agent_id == query.agent_id)
            if query.type:
                stmt = stmt.where(Memory.type == query.type)
                count_stmt = count_stmt.where(Memory.type == query.type)

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
        """Create a new :class:`Memory` row (with a computed embedding).

        P1-4/P2-5/P0-2 fix: embed off the event loop (``embed_text`` makes a
        blocking 30s-timeout HTTP call to ollama that would otherwise freeze
        the whole loop), embed ``content+summary`` to match
        ``MemoryService.create`` (so the same memory gets the same vector no
        matter which path stored it), and tolerate an embedding failure by
        storing ``NULL`` instead of letting it raise — mirroring create()'s
        null-embedding safety. Since P0-2 made ``embed_text`` raise on ollama
        failure (rather than poisoning the column with a hash vector), this
        guard is what stops that from hard-failing the store.
        """
        text = memory_embedding_text(data.content, data.summary)
        try:
            embedding = await asyncio.to_thread(embed_text, text)
        except Exception:
            logger.exception(
                "MemoryProvider.store: embed_text failed — storing without embedding"
            )
            embedding = None
        memory = Memory(
            user_id=data.user_id,
            agent_id=data.agent_id,  # P1-3: was dropped → every SDK-stored row
            # silently landed in agent_id="default"; now honours the caller's scope.
            type=data.type,
            content=data.content,
            summary=data.summary,
            importance=data.importance,
            tags=list(data.tags or []),
            source=data.source,
            status=MemoryStatus.ACTIVE,
            embedding=embedding,
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

            if data.summary is not None:
                memory.summary = data.summary
            if data.importance is not None:
                memory.importance = data.importance
            if data.tags is not None:
                memory.tags = list(data.tags)
            if data.status is not None:
                memory.status = data.status

            # Recompute the embedding only when content changed, off the event
            # loop, from the final content+summary (matching store()/create()),
            # tolerating failure with a NULL rather than poisoning the column
            # or hard-failing the update (see store() for the full rationale).
            if data.content is not None:
                memory.content = data.content
                text = memory_embedding_text(memory.content, memory.summary)
                try:
                    memory.embedding = await asyncio.to_thread(embed_text, text)
                except Exception:
                    logger.exception(
                        "MemoryProvider.update: embed_text failed for %s — leaving embedding NULL",
                        data.id,
                    )
                    memory.embedding = None

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
            memory.status = MemoryStatus.ARCHIVED
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
        except Exception:
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
            capabilities=["search", "hybrid_search", "store", "update", "delete", "embedding"],
            config={"backend": self._settings.memory_provider},
        )

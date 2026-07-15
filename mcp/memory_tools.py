"""MCP memory tool implementations.

These are the async functions exposed by the HCC MCP server (see ``server.py``).
They reuse the *existing* gateway building blocks without modifying them:

* :mod:`gateway.core.database` — the async engine / session factory (same DB).
* :mod:`gateway.models`        — the :class:`Memory` ORM model.
* :mod:`gateway.core.embeddings` — the hash-based ``embed_text`` embedder used
  for semantic search (and to populate embeddings on store).

Every tool returns a plain, JSON-serialisable ``dict`` with a ``success`` flag
so MCP clients get structured data plus predictable error handling.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import delete as sql_delete
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

# Reuse the gateway's async engine, session factory and Base metadata.
from gateway.core.database import Base, async_session, engine
from gateway.core.embeddings import embed_text
from gateway.models import Memory

__all__ = [
    "store_memory",
    "search_memories",
    "semantic_search",
    "get_recent_memories",
    "delete_memory",
]

# ---------------------------------------------------------------------------
# Database bootstrap
# ---------------------------------------------------------------------------
# The MCP server does not run FastAPI's lifespan hook, so we lazily make sure
# the pgvector extension and tables exist before the first query. Guarded by a
# lock + flag so it only ever runs once per process.
_db_ready = False
_db_lock = asyncio.Lock()


async def ensure_db() -> None:
    """Create the pgvector extension and ORM tables if they do not exist."""
    global _db_ready
    if _db_ready:
        return
    async with _db_lock:
        if _db_ready:
            return
        async with engine.begin() as conn:
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            await conn.run_sync(Base.metadata.create_all)
        _db_ready = True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _serialize(memory: Memory) -> dict[str, Any]:
    """Turn a :class:`Memory` row into a JSON-serialisable dict."""

    def _iso(value: Any) -> Optional[str]:
        return value.isoformat() if isinstance(value, datetime) else value

    return {
        "id": memory.id,
        "user_id": memory.user_id,
        "type": memory.type,
        "content": memory.content,
        "summary": memory.summary or "",
        "importance": memory.importance,
        "tags": list(memory.tags or []),
        "source": memory.source,
        "status": memory.status,
        "created_at": _iso(memory.created_at),
        "updated_at": _iso(memory.updated_at),
    }


def _ok(**payload: Any) -> dict[str, Any]:
    return {"success": True, **payload}


def _err(message: str) -> dict[str, Any]:
    return {"success": False, "error": message}


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------
async def store_memory(
    content: str,
    user_id: str = "default",
    type: str = "general",
    summary: str = "",
    importance: float = 0.5,
    tags: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Store a new memory.

    An embedding is generated with the hash-based embedder so the memory is
    immediately discoverable via ``semantic_search``.
    """
    try:
        if not content or not content.strip():
            return _err("content must not be empty")

        importance = max(0.0, min(1.0, float(importance)))
        tags = list(tags) if tags else []

        await ensure_db()
        async with async_session() as session:  # type: AsyncSession
            memory = Memory(
                user_id=user_id,
                type=type,
                content=content,
                summary=summary or "",
                importance=importance,
                tags=tags,
                source="mcp",
                embedding=embed_text(content),
            )
            session.add(memory)
            await session.commit()
            await session.refresh(memory)
            return _ok(memory=_serialize(memory))
    except Exception as exc:  # noqa: BLE001 - surface any error as structured data
        return _err(f"{exc.__class__.__name__}: {exc}")


async def search_memories(
    query: str,
    user_id: Optional[str] = None,
    type: Optional[str] = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Keyword search over memory ``content`` and ``summary`` (case-insensitive)."""
    try:
        limit = max(1, min(100, int(limit)))
        await ensure_db()
        async with async_session() as session:
            stmt = select(Memory)
            count_stmt = select(func.count(Memory.id))

            if user_id:
                stmt = stmt.where(Memory.user_id == user_id)
                count_stmt = count_stmt.where(Memory.user_id == user_id)
            if type:
                stmt = stmt.where(Memory.type == type)
                count_stmt = count_stmt.where(Memory.type == type)
            if query:
                like = f"%{query}%"
                cond = Memory.content.ilike(like) | Memory.summary.ilike(like)
                stmt = stmt.where(cond)
                count_stmt = count_stmt.where(cond)

            stmt = stmt.order_by(Memory.created_at.desc()).limit(limit)

            total = (await session.execute(count_stmt)).scalar() or 0
            rows = (await session.execute(stmt)).scalars().all()
            return _ok(
                total=int(total),
                count=len(rows),
                results=[_serialize(m) for m in rows],
            )
    except Exception as exc:  # noqa: BLE001
        return _err(f"{exc.__class__.__name__}: {exc}")


async def semantic_search(
    query_text: str,
    limit: int = 10,
    user_id: Optional[str] = None,
) -> dict[str, Any]:
    """Vector similarity search using the hash-based embedder + pgvector cosine distance."""
    try:
        if not query_text or not query_text.strip():
            return _err("query_text must not be empty")

        limit = max(1, min(100, int(limit)))
        embedding = embed_text(query_text)

        await ensure_db()
        async with async_session() as session:
            distance = Memory.embedding.cosine_distance(embedding).label("distance")
            stmt = select(Memory, distance).where(Memory.embedding.is_not(None))
            if user_id:
                stmt = stmt.where(Memory.user_id == user_id)
            stmt = stmt.order_by(distance).limit(limit)

            rows = (await session.execute(stmt)).all()
            results = []
            for memory, dist in rows:
                item = _serialize(memory)
                # Cosine distance in [0, 2]; convert to a [0, 1] similarity score.
                item["distance"] = float(dist)
                item["score"] = round(1.0 - float(dist) / 2.0, 6)
                results.append(item)
            return _ok(count=len(results), results=results)
    except Exception as exc:  # noqa: BLE001
        return _err(f"{exc.__class__.__name__}: {exc}")


async def get_recent_memories(
    limit: int = 20,
    user_id: Optional[str] = None,
) -> dict[str, Any]:
    """Return the most recently created memories, optionally scoped to a user."""
    try:
        limit = max(1, min(100, int(limit)))
        await ensure_db()
        async with async_session() as session:
            stmt = select(Memory)
            count_stmt = select(func.count(Memory.id))
            if user_id:
                stmt = stmt.where(Memory.user_id == user_id)
                count_stmt = count_stmt.where(Memory.user_id == user_id)
            stmt = stmt.order_by(Memory.created_at.desc()).limit(limit)

            total = (await session.execute(count_stmt)).scalar() or 0
            rows = (await session.execute(stmt)).scalars().all()
            return _ok(
                total=int(total),
                count=len(rows),
                results=[_serialize(m) for m in rows],
            )
    except Exception as exc:  # noqa: BLE001
        return _err(f"{exc.__class__.__name__}: {exc}")


async def delete_memory(memory_id: str) -> dict[str, Any]:
    """Delete a memory by id. Returns ``success=False`` if nothing was deleted."""
    try:
        if not memory_id:
            return _err("memory_id is required")
        await ensure_db()
        async with async_session() as session:
            result = await session.execute(
                sql_delete(Memory).where(Memory.id == memory_id)
            )
            await session.commit()
            if result.rowcount and result.rowcount > 0:
                return _ok(deleted=True, memory_id=memory_id)
            return _err(f"memory not found: {memory_id}")
    except Exception as exc:  # noqa: BLE001
        return _err(f"{exc.__class__.__name__}: {exc}")

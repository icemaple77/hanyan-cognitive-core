"""MCP tool implementations for HCC.

2026-08 重写:原版直接拼 SQL + 用 ``gateway.core.embeddings`` 里的"哈希嵌入"
占位实现(和真正接入的 Qwen3-Embedding 是两套不兼容的向量空间,混进同一
pgvector 列会让语义检索的余弦距离比较失真),而且原版 import 的
``mcp.server.fastmcp`` 路径在装的 mcp 2.0 SDK 里已经不存在——这个模块
从写完起就没有真正跑通过一次。

重写原则:不再自己发明一套查询逻辑,直接复用 ``gateway.services``/
``core.*`` ——FastAPI 路由也在用的同一份代码,同进程内调用。这样这几个月
在 gateway 侧修的所有 bug(agent_id 过滤、时区、中文去重、情感关键词等)
对 MCP 这条路自动生效,不会分裂出第二套不同步的实现。

嵌入:HCC 跑在 aicore(无 GPU/MLX),算不出真嵌入。``store_memory``/
``semantic_search`` 都改成"调用方可选传入预计算好的向量"(和 REST API 的
``/memory/store`` 一致),不再自己伪造一个哈希向量出来污染同一列——
没传向量就诚实地只能被关键词/三层检索找到,好过悄悄存一个不兼容的假向量。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from gateway.core.database import async_session
from gateway.core.events import publish_memory_event
from gateway.models import Memory
from gateway.schemas.memory import MemoryCreate, MemorySearch
from gateway.services import MemoryService
from core.orchestrator import get_orchestrator
from core.subconscious import get_subconscious

__all__ = [
    "store_memory",
    "search_memories",
    "recall",
    "semantic_search",
    "hybrid_search",
    "get_recent_memories",
    "delete_memory",
    "evaluate",
]


def _serialize(memory: Memory) -> dict[str, Any]:
    def _iso(value: Any) -> Optional[str]:
        return value.isoformat() if isinstance(value, datetime) else value

    return {
        "id": memory.id,
        "user_id": memory.user_id,
        "agent_id": memory.agent_id,
        "type": memory.type,
        "content": memory.content,
        "summary": memory.summary or "",
        "importance": memory.importance,
        "tags": list(memory.tags or []),
        "status": memory.status,
        "access_count": memory.access_count,
        "created_at": _iso(memory.created_at),
        "updated_at": _iso(memory.updated_at),
    }


def _ok(**payload: Any) -> dict[str, Any]:
    return {"success": True, **payload}


def _err(message: str) -> dict[str, Any]:
    return {"success": False, "error": message}


async def store_memory(
    content: str,
    user_id: str = "default",
    agent_id: str = "default",
    type: str = "general",
    summary: str = "",
    importance: float = 0.5,
    tags: Optional[list[str]] = None,
    embedding: Optional[list[float]] = None,
) -> dict[str, Any]:
    """Store a new memory.

    embedding: 可选。调用方(有嵌入模型的一端,例如接了 Mac 侧 Qwen3-Embedding
    的客户端)可以预先算好向量传进来;不传就只能被关键词/三层检索找到。
    """
    try:
        if not content or not content.strip():
            return _err("content must not be empty")
        importance = max(0.0, min(1.0, float(importance)))

        async with async_session() as session:
            data = MemoryCreate(
                user_id=user_id, agent_id=agent_id, type=type, content=content,
                summary=summary or "", importance=importance, tags=list(tags) if tags else [],
                source="mcp", embedding=embedding,
            )
            service = MemoryService(session)
            memory = await service.create(data)
            await session.commit()
            await publish_memory_event("store", memory.id, user_id=memory.user_id)
            return _ok(memory=_serialize(memory))
    except Exception as exc:  # noqa: BLE001 - surface any error as structured data
        return _err(f"{exc.__class__.__name__}: {exc}")


async def search_memories(
    query: str,
    user_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    type: Optional[str] = None,
    limit: int = 20,
) -> dict[str, Any]:
    """关键词(ILIKE 子串)检索,不需要嵌入模型,任何 MCP 客户端都能用。"""
    try:
        limit = max(1, min(100, int(limit)))
        async with async_session() as session:
            service = MemoryService(session)
            q = MemorySearch(query=query, user_id=user_id, agent_id=agent_id, type=type, limit=limit)
            memories, total = await service.search(q)
            return _ok(total=total, count=len(memories), results=[_serialize(m) for m in memories])
    except Exception as exc:  # noqa: BLE001
        return _err(f"{exc.__class__.__name__}: {exc}")


async def recall(
    query: str,
    user_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    limit: int = 5,
) -> dict[str, Any]:
    """三层检索(意识/前意识/潜意识),比纯关键词更贴合"想起了什么"。不需要嵌入模型。"""
    try:
        async with async_session() as session:
            service = MemoryService(session)

            class _KeywordProvider:
                async def search(self, query: str, user_id=None, agent_id=None, limit: int = 10):
                    q = MemorySearch(query=query, user_id=user_id, agent_id=agent_id, limit=limit)
                    memories, _ = await service.search(q)
                    return {"items": [_serialize(m) for m in memories]}

            sub = get_subconscious()
            sub.add_to_conscious(query, "mcp")
            results = await sub.retrieve(query, memory_provider=_KeywordProvider(), limit=limit,
                                         user_id=user_id, agent_id=agent_id)
            return _ok(results=[
                {"content": r.content, "source": r.source, "score": r.score,
                 "importance": r.importance, "memory_id": r.memory_id}
                for r in results
            ])
    except Exception as exc:  # noqa: BLE001
        return _err(f"{exc.__class__.__name__}: {exc}")


async def semantic_search(
    embedding: list[float],
    user_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    type: Optional[str] = None,
    limit: int = 10,
) -> dict[str, Any]:
    """语义相似度检索(pgvector cosine)。需要调用方传入预计算好的向量——
    HCC 自己算不出来(aicore 没有 GPU/MLX)。"""
    try:
        if not embedding:
            return _err("embedding is required — HCC 不能自己计算向量,调用方需预先算好传入")
        limit = max(1, min(100, int(limit)))
        async with async_session() as session:
            service = MemoryService(session)
            results = await service.semantic_search(embedding=embedding, limit=limit,
                                                     user_id=user_id, agent_id=agent_id, type=type)
            items = []
            for memory, dist in results:
                item = _serialize(memory)
                item["distance"] = float(dist)
                item["score"] = round(1.0 - float(dist) / 2.0, 6)
                items.append(item)
            return _ok(count=len(items), results=items)
    except Exception as exc:  # noqa: BLE001
        return _err(f"{exc.__class__.__name__}: {exc}")


async def hybrid_search(
    query: str = "",
    embedding: Optional[list[float]] = None,
    user_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    type: Optional[str] = None,
    limit: int = 10,
    rerank: bool = False,
) -> dict[str, Any]:
    """BM25 全文 + 向量(RRF融合)混合检索。embedding 可选(不传就退化成纯 BM25);
    query 也可选(不传就退化成纯向量)。至少给一个。"""
    try:
        if not query and not embedding:
            return _err("must provide at least one of query or embedding")
        limit = max(1, min(100, int(limit)))
        async with async_session() as session:
            service = MemoryService(session)
            results = await service.hybrid_search(
                query=query, embedding=embedding, limit=limit,
                user_id=user_id, agent_id=agent_id, type=type, rerank=rerank,
            )
            items = []
            for item in results:
                serialized = _serialize(item["memory"])
                serialized["rrf_score"] = item["rrf_score"]
                serialized["bm25_rank"] = item.get("bm25_rank")
                serialized["vector_rank"] = item.get("vector_rank")
                serialized["rerank_score"] = item.get("rerank_score")
                items.append(serialized)
            return _ok(count=len(items), results=items)
    except Exception as exc:  # noqa: BLE001
        return _err(f"{exc.__class__.__name__}: {exc}")


async def get_recent_memories(
    limit: int = 20,
    user_id: Optional[str] = None,
    agent_id: Optional[str] = None,
) -> dict[str, Any]:
    """最近的记忆,可按 user_id/agent_id 过滤。"""
    try:
        limit = max(1, min(100, int(limit)))
        async with async_session() as session:
            service = MemoryService(session)
            q = MemorySearch(query="", user_id=user_id, agent_id=agent_id, limit=limit)
            memories, total = await service.search(q)
            return _ok(total=total, count=len(memories), results=[_serialize(m) for m in memories])
    except Exception as exc:  # noqa: BLE001
        return _err(f"{exc.__class__.__name__}: {exc}")


async def delete_memory(memory_id: str) -> dict[str, Any]:
    """删除一条记忆(物理删除)。想要"遗忘但保留"应该用归档(forget/apply),不是这个。"""
    try:
        if not memory_id:
            return _err("memory_id is required")
        async with async_session() as session:
            service = MemoryService(session)
            ok = await service.delete(memory_id)
            await session.commit()
            if ok:
                await publish_memory_event("delete", memory_id)
                return _ok(deleted=True, memory_id=memory_id)
            return _err(f"memory not found: {memory_id}")
    except Exception as exc:  # noqa: BLE001
        return _err(f"{exc.__class__.__name__}: {exc}")


async def evaluate(content: str, agent_id: str = "default", user_id: str = "default") -> dict[str, Any]:
    """该不该记这句话(orchestrator 判断),供调用方在存储前先问一句,别把闲聊也记进长期记忆。"""
    try:
        orchestrator = get_orchestrator()
        decision = orchestrator.evaluate(content, "mcp", user_id)
        return _ok(should_store=decision.should_store, importance=decision.importance,
                  reason=decision.reason, suggested_tags=decision.suggested_tags)
    except Exception as exc:  # noqa: BLE001
        return _err(f"{exc.__class__.__name__}: {exc}")

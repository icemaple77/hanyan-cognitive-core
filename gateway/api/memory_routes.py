"""Memory CRUD routes."""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.core.database import get_session
from gateway.models import Memory
from gateway.schemas.memory import (
    MemoryCreate,
    MemoryUpdate,
    MemoryResponse,
    MemorySearch,
    MemoryListResponse,
    SemanticSearchRequest,
)
from gateway.services import MemoryService

router = APIRouter()


@router.post("/memory/store", response_model=MemoryResponse)
async def store_memory(
    data: MemoryCreate,
    session: AsyncSession = Depends(get_session),
) -> MemoryResponse:
    service = MemoryService(session)
    memory = await service.create(data)
    return MemoryResponse.model_validate(memory)


@router.post("/memory/search", response_model=MemoryListResponse)
async def search_memories(
    query: MemorySearch,
    session: AsyncSession = Depends(get_session),
) -> MemoryListResponse:
    service = MemoryService(session)
    memories, total = await service.search(query)
    return MemoryListResponse(
        items=[MemoryResponse.model_validate(m) for m in memories],
        total=total,
    )


@router.post("/memory/update", response_model=MemoryResponse)
async def update_memory(
    data: MemoryUpdate,
    session: AsyncSession = Depends(get_session),
) -> MemoryResponse:
    service = MemoryService(session)
    memory = await service.update(data)
    if memory is None:
        raise HTTPException(status_code=404, detail="Memory not found")
    return MemoryResponse.model_validate(memory)


@router.post("/memory/delete")
async def delete_memory(
    memory_id: str,
    session: AsyncSession = Depends(get_session),
) -> dict:
    service = MemoryService(session)
    success = await service.delete(memory_id)
    if not success:
        raise HTTPException(status_code=404, detail="Memory not found")
    return {"status": "ok", "message": "Memory deleted"}


@router.get("/memory/recent", response_model=MemoryListResponse)
async def recent_memories(
    limit: int = 20,
    offset: int = 0,
    session: AsyncSession = Depends(get_session),
) -> MemoryListResponse:
    service = MemoryService(session)
    memories, total = await service.get_recent(limit=limit, offset=offset)
    return MemoryListResponse(
        items=[MemoryResponse.model_validate(m) for m in memories],
        total=total,
    )


class TouchRequest(BaseModel):
    ids: list[str]


@router.post("/memory/touch", summary="Recall reinforces memory (access_count+1, last_access=now)")
async def touch_memories(data: TouchRequest, session: AsyncSession = Depends(get_session)) -> dict:
    """检索命中即"复述":access_count+1、last_access刷新 —— 遗忘引擎靠这个数据判断
    "常被谈起的事该记牢"。被检索到却从不 touch,forget_score 就只会随时间单调下降,
    等于遗忘机制形同虚设(半年前就是这个状态)。"""
    if not data.ids:
        return {"touched": 0}
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    stmt = (
        sa_update(Memory)
        .where(Memory.id.in_(data.ids))
        .values(access_count=Memory.access_count + 1, last_access=now)
    )
    result = await session.execute(stmt)
    await session.commit()
    return {"touched": result.rowcount}


@router.post("/memory/semantic-search", response_model=MemoryListResponse)
async def semantic_search_memories(
    query: SemanticSearchRequest,
    session: AsyncSession = Depends(get_session),
) -> MemoryListResponse:
    service = MemoryService(session)
    results = await service.semantic_search(
        embedding=query.embedding,
        limit=query.limit,
        user_id=query.user_id,
        agent_id=query.agent_id,
        type=query.type,
    )
    return MemoryListResponse(
        items=[MemoryResponse.model_validate(m) for m, _ in results],
        total=len(results),
    )

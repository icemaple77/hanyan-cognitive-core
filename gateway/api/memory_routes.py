"""Memory CRUD routes."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.core.database import get_session
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
    )
    return MemoryListResponse(
        items=[MemoryResponse.model_validate(m) for m, _ in results],
        total=len(results),
    )

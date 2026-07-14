"""Memory business logic service."""

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select, func, delete
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.models.memory import Memory
from gateway.schemas.memory import MemoryCreate, MemoryUpdate, MemorySearch


class MemoryService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(self, data: MemoryCreate) -> Memory:
        memory = Memory(**data.model_dump())
        self.session.add(memory)
        await self.session.flush()
        return memory

    async def search(self, query: MemorySearch) -> tuple[list[Memory], int]:
        stmt = select(Memory)
        count_stmt = select(func.count(Memory.id))

        if query.user_id:
            stmt = stmt.where(Memory.user_id == query.user_id)
            count_stmt = count_stmt.where(Memory.user_id == query.user_id)
        if query.type:
            stmt = stmt.where(Memory.type == query.type)
            count_stmt = count_stmt.where(Memory.type == query.type)
        if query.query:
            like = f"%{query.query}%"
            stmt = stmt.where(Memory.content.ilike(like) | Memory.summary.ilike(like))
            count_stmt = count_stmt.where(Memory.content.ilike(like) | Memory.summary.ilike(like))

        stmt = stmt.order_by(Memory.created_at.desc()).offset(query.offset).limit(query.limit)

        total_result = await self.session.execute(count_stmt)
        total = total_result.scalar() or 0

        result = await self.session.execute(stmt)
        memories = list(result.scalars().all())

        return memories, total

    async def update(self, data: MemoryUpdate) -> Optional[Memory]:
        memory = await self.session.get(Memory, data.id)
        if memory is None:
            return None

        update_data = data.model_dump(exclude_unset=True, exclude={"id"})
        for key, value in update_data.items():
            setattr(memory, key, value)
        memory.updated_at = datetime.now(timezone.utc)

        await self.session.flush()
        return memory

    async def delete(self, memory_id: str) -> bool:
        result = await self.session.execute(delete(Memory).where(Memory.id == memory_id))
        return result.rowcount > 0

    async def get_recent(self, limit: int = 20, offset: int = 0) -> tuple[list[Memory], int]:
        stmt = select(Memory).order_by(Memory.created_at.desc()).offset(offset).limit(limit)
        count_stmt = select(func.count(Memory.id))

        total_result = await self.session.execute(count_stmt)
        total = total_result.scalar() or 0

        result = await self.session.execute(stmt)
        memories = list(result.scalars().all())

        return memories, total

"""Priority Compass routes —— 公子价值坐标的登记通道(REST 面)。

抄 task_routes.py 的形。三方运行时 + 小屏共用:REST 给外部/HUD,MCP priority_*
给会话内 agent。两者打同一 :class:`PriorityService`,行为不漂移。
设计见 docs/priority-compass-design.md §六。
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.core.database import get_session
from gateway.services.priority_service import PriorityService

router = APIRouter()


class PrioritySetRequest(BaseModel):
    label: str
    importance: int = 3       # 1-5
    urgency: int = 3          # 1-5
    anchors: list[str] = Field(default_factory=list)
    source: str = "gongzi"    # gongzi | agent:<name>
    trust: Optional[str] = None  # None → gongzi=confirmed / agent=pending
    review_at: Optional[str] = None  # ISO date;复核日
    user_id: str = "michael"


@router.post("/priorities", summary="登记/更新一条价值坐标(重要性×紧急性)")
async def set_priority(req: PrioritySetRequest, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    svc = PriorityService(session)
    try:
        p = await svc.set(
            label=req.label, importance=req.importance, urgency=req.urgency,
            anchors=req.anchors, source=req.source, trust=req.trust,
            review_at=req.review_at, user_id=req.user_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    await session.commit()
    return {"priority": p}


@router.get("/priorities", summary="列出价值坐标(默认只列 active)")
async def list_priorities(user_id: str = "michael", status: Optional[str] = "active",
                          session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    svc = PriorityService(session)
    return {"priorities": await svc.list(user_id=user_id, status=status)}


@router.post("/priorities/{priority_id}/confirm", summary="把 pending 提案转正为 confirmed")
async def confirm_priority(priority_id: str, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    svc = PriorityService(session)
    p = await svc.confirm(priority_id)
    if p is None:
        raise HTTPException(status_code=404, detail="priority not found")
    await session.commit()
    return {"priority": p}


@router.post("/priorities/{priority_id}/retire", summary="退役一条(不物删;可记版本链)")
async def retire_priority(priority_id: str, superseded_by: Optional[str] = None,
                          session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    svc = PriorityService(session)
    p = await svc.retire(priority_id, superseded_by=superseded_by)
    if p is None:
        raise HTTPException(status_code=404, detail="priority not found")
    await session.commit()
    return {"priority": p}

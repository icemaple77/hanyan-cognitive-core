"""Task-Schedule routes — agent long-task anti-stall (看板卡 t_6b29b140).

REST surface over :class:`gateway.services.task_service.TaskService`, mainly for
the per-runtime cron driver (which polls ``GET /tasks/due`` and, per due task,
spawns a fresh agent session that acts on ``POST /tasks/{id}/wake``). Agents in
an active session usually drive the same state machine via the MCP task_* tools
instead. Both hit the identical service, so behaviour can't drift between them.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.core.database import get_session
from gateway.services.task_service import TaskService

router = APIRouter()


class TaskStepIn(BaseModel):
    title: str
    instruction: str = ""
    verify_cmd: str = ""
    est_seconds: int = 0  # 0 → server calibrates from history


class TaskCreateRequest(BaseModel):
    title: str
    steps: list[TaskStepIn]
    goal: str = ""
    user_id: str = "michael"
    agent_id: str = "default"
    redline_tags: list[str] = Field(default_factory=list)


class TaskReportRequest(BaseModel):
    step_idx: int
    verified_done: bool
    actual_seconds: Optional[int] = None
    note: str = ""


@router.post("/tasks", summary="Register a long task (steps + estimates)")
async def create_task(req: TaskCreateRequest, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    if not req.steps:
        raise HTTPException(status_code=422, detail="a task needs at least one step")
    svc = TaskService(session)
    task = await svc.register(
        user_id=req.user_id, agent_id=req.agent_id, title=req.title, goal=req.goal,
        steps=[s.model_dump() for s in req.steps], redline_tags=req.redline_tags,
    )
    await session.commit()
    return {"task": task}


@router.get("/tasks/due", summary="List tasks whose wake is due now (cron polls this)")
async def due_tasks(agent_id: Optional[str] = None, limit: int = 20,
                    session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    due = await TaskService(session).list_due(agent_id=agent_id, limit=limit)
    return {"count": len(due), "tasks": due}


@router.get("/tasks/{task_id}", summary="Fetch a task and its steps")
async def get_task(task_id: str, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    task = await TaskService(session).get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"task not found: {task_id}")
    return {"task": task}


@router.post("/tasks/{task_id}/wake", summary="Get current-step marching orders for a woken session")
async def wake_task(task_id: str, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    payload = await TaskService(session).wake_payload(task_id)
    await session.commit()  # wake bumps attempts / last_heartbeat / status
    if payload.get("error"):
        raise HTTPException(status_code=404, detail=payload["error"])
    return payload


@router.post("/tasks/{task_id}/report", summary="Report deterministic step result")
async def report_task(task_id: str, req: TaskReportRequest,
                      session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    result = await TaskService(session).report(
        task_id=task_id, step_idx=req.step_idx, verified_done=req.verified_done,
        actual_seconds=req.actual_seconds, note=req.note,
    )
    await session.commit()
    if result.get("error"):
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@router.post("/tasks/{task_id}/cancel", summary="Cancel a task (stop future wakes)")
async def cancel_task(task_id: str, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    ok = await TaskService(session).cancel(task_id)
    await session.commit()
    if not ok:
        raise HTTPException(status_code=404, detail=f"task not found: {task_id}")
    return {"cancelled": True, "task_id": task_id}

"""MCP tools for Task-Schedule (agent long-task anti-stall).

Thin async wrappers over :class:`gateway.services.task_service.TaskService`,
same in-process pattern as :mod:`mcp.memory_tools` (open a short-lived
``async_session``, commit on success, surface any error as structured data).

Typical agent flow:
    1. task_create(...)  — decompose a long task into steps+estimates, register it.
    2. ... do step 0 now ...
    3. task_report(task_id, step_idx=0, verified_done=True, actual_seconds=...)
    When a cron later wakes a fresh session, that session calls task_wake(task_id)
    to get its marching orders, runs the verify_cmd, then task_report(...).
"""

from __future__ import annotations

from typing import Any, Optional

from gateway.core.database import async_session
from gateway.services.task_service import TaskService

__all__ = [
    "task_create", "task_get", "task_due", "task_wake", "task_report", "task_cancel",
]


def _ok(**payload: Any) -> dict[str, Any]:
    return {"success": True, **payload}


def _err(message: str) -> dict[str, Any]:
    return {"success": False, "error": message}


async def task_create(
    title: str,
    steps: list[dict[str, Any]],
    goal: str = "",
    user_id: str = "michael",
    agent_id: str = "default",
    redline_tags: Optional[list[str]] = None,
    repeat: Optional[str] = None,
) -> dict[str, Any]:
    """Register a long task, decomposed into ordered steps, and schedule its first
    wake (the safety net if this session stalls).

    steps: list of {title, instruction, verify_cmd, est_seconds}. verify_cmd is a
    shell command the woken session runs to check progress deterministically
    (e.g. "tail -50 ~/train.log | grep -c 'epoch 100'"). est_seconds drives the
    wake interval; pass 0 to let the server calibrate from history.
    repeat: 循环任务(非空则永不终态 DONE,验完重置回第 0 步)。形式:
      "every:6h" / "every:1d" / "daily:09:00" / 纯秒数;None=一次性任务。
    """
    try:
        if not title or not title.strip():
            return _err("title must not be empty")
        if not steps:
            return _err("a task needs at least one step")
        async with async_session() as session:
            svc = TaskService(session)
            task = await svc.register(
                user_id=user_id, agent_id=agent_id, title=title, goal=goal,
                steps=steps, redline_tags=redline_tags, repeat=repeat,
            )
            await session.commit()
            return _ok(task=task)
    except Exception as exc:
        return _err(f"{exc.__class__.__name__}: {exc}")


async def task_get(task_id: str) -> dict[str, Any]:
    """Fetch a task and all its steps."""
    try:
        async with async_session() as session:
            task = await TaskService(session).get(task_id)
            if task is None:
                return _err(f"task not found: {task_id}")
            return _ok(task=task)
    except Exception as exc:
        return _err(f"{exc.__class__.__name__}: {exc}")


async def task_due(agent_id: Optional[str] = None, limit: int = 20) -> dict[str, Any]:
    """List tasks whose wake is due now (for the per-runtime cron driver to poll)."""
    try:
        async with async_session() as session:
            due = await TaskService(session).list_due(agent_id=agent_id, limit=limit)
            return _ok(count=len(due), tasks=due)
    except Exception as exc:
        return _err(f"{exc.__class__.__name__}: {exc}")


async def task_wake(task_id: str) -> dict[str, Any]:
    """Get the marching orders for a task's current step (call this from a freshly
    woken session). Enforces the attempt cap and redline escalation, so the
    returned ``action`` may be "work", "escalate", or "none"."""
    try:
        async with async_session() as session:
            payload = await TaskService(session).wake_payload(task_id)
            await session.commit()  # wake bumps attempts/last_heartbeat/status
            if "error" in payload:
                return _err(payload["error"])
            return _ok(**payload)
    except Exception as exc:
        return _err(f"{exc.__class__.__name__}: {exc}")


async def task_report(
    task_id: str,
    step_idx: int,
    verified_done: bool,
    actual_seconds: Optional[int] = None,
    note: str = "",
) -> dict[str, Any]:
    """Report the deterministic result of the current step (after running its
    verify_cmd). verified_done=True advances to the next step (or finishes the
    task); False re-estimates and reschedules the next wake."""
    try:
        async with async_session() as session:
            result = await TaskService(session).report(
                task_id=task_id, step_idx=step_idx, verified_done=verified_done,
                actual_seconds=actual_seconds, note=note,
            )
            await session.commit()
            if "error" in result:
                return _err(result["error"])
            return _ok(**result)
    except Exception as exc:
        return _err(f"{exc.__class__.__name__}: {exc}")


async def task_cancel(task_id: str) -> dict[str, Any]:
    """Cancel a task (stops all future wakes)."""
    try:
        async with async_session() as session:
            ok = await TaskService(session).cancel(task_id)
            await session.commit()
            return _ok(cancelled=ok, task_id=task_id) if ok else _err(f"task not found: {task_id}")
    except Exception as exc:
        return _err(f"{exc.__class__.__name__}: {exc}")

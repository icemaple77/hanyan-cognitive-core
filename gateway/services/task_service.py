"""Task-Schedule service — the agent long-task anti-stall state machine.

See the models header in :mod:`gateway.models` for the why. In short: a fresh,
zero-memory agent session, woken by an external cron, must be able to resume a
long task exactly where it left off and push it one step forward without ever
sitting idle waiting for a human. This service owns that state machine:

    register()      agent decomposes a long task into steps+estimates, stores it,
                    schedules the first wake as a safety net.
    due()           the per-runtime cron polls this to find tasks whose wake is due.
    wake_payload()  render the instruction a fresh woken session should act on
                    (goal + current step + verify_cmd + "unattended, don't wait"),
                    enforcing the attempt cap and the redline escalation.
    report()        the woken session ran verify_cmd in its own environment and
                    reports the *deterministic* result → advance to the next step,
                    or re-estimate and reschedule.

Determinism note: progress is never taken from an agent's self-narration. The
woken session has no memory of the prior run; it re-derives "am I done?" by
running ``TaskStep.verify_cmd`` against the real world (logs/artifacts) and
reports that. This service only trusts ``verified_done`` gathered that way.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.models import Task, TaskStep, TaskStatus, StepStatus

logger = logging.getLogger(__name__)

# A step still unfinished after this many wakes → BLOCKED + human escalation,
# rather than being re-woken forever (catches both "agent keeps failing" and
# "agent died and never reports"). Deliberately small.
MAX_ATTEMPTS = 3

# When a wake finds a step unfinished, the next wake is pushed out by this factor
# (agents systematically under-estimate; a fixed re-poll at the same interval
# would just cry wolf), capped so a heartbeat never sleeps longer than this.
WAKE_BACKOFF = 1.5
MAX_WAKE_SECONDS = 2 * 60 * 60  # 2h ceiling on any single heartbeat interval
MIN_WAKE_SECONDS = 30

# Global redline: a step whose instruction mentions any of these blocks the task
# for a human decision instead of auto-advancing (unattended mode never crosses
# these on its own). Per-task extras come from Task.redline_tags.
GLOBAL_REDLINE = [
    "删除", "drop table", "rm -rf", "花钱", "支付", "付款", "转账", "purchase", "payment",
    "对外发送", "发邮件", "send email", "公开发布", "publish", "deploy 生产", "生产环境",
    "家人", "家庭", "老婆", "孩子",
]


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _clamp_wake(seconds: float) -> int:
    return int(max(MIN_WAKE_SECONDS, min(MAX_WAKE_SECONDS, seconds)))


def _step_to_dict(s: TaskStep) -> dict[str, Any]:
    return {
        "id": s.id, "idx": s.idx, "title": s.title, "instruction": s.instruction,
        "verify_cmd": s.verify_cmd, "est_seconds": s.est_seconds,
        "actual_seconds": s.actual_seconds, "attempts": s.attempts, "status": s.status,
    }


def _task_to_dict(t: Task, steps: Optional[list[TaskStep]] = None) -> dict[str, Any]:
    d = {
        "id": t.id, "user_id": t.user_id, "agent_id": t.agent_id, "title": t.title,
        "goal": t.goal, "status": t.status, "current_step": t.current_step,
        "attempts_on_current": t.attempts_on_current,
        "redline_tags": list(t.redline_tags or []),
        "next_wake_at": t.next_wake_at.isoformat() if t.next_wake_at else None,
        "last_heartbeat": t.last_heartbeat.isoformat() if t.last_heartbeat else None,
        "note": t.note or "",
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "updated_at": t.updated_at.isoformat() if t.updated_at else None,
    }
    if steps is not None:
        d["steps"] = [_step_to_dict(s) for s in steps]
    return d


class TaskService:
    def __init__(self, session: AsyncSession):
        self.session = session

    # ------------------------------------------------------------------
    async def _steps(self, task_id: str) -> list[TaskStep]:
        rows = await self.session.execute(
            select(TaskStep).where(TaskStep.task_id == task_id).order_by(TaskStep.idx)
        )
        return list(rows.scalars().all())

    def _redline_hit(self, task: Task, instruction: str) -> Optional[str]:
        text = (instruction or "").lower()
        for kw in [*GLOBAL_REDLINE, *(task.redline_tags or [])]:
            if kw and str(kw).lower() in text:
                return str(kw)
        return None

    async def _calibrated_est(self, agent_id: str, title: str, fallback: int) -> int:
        """History-based estimate: mean actual_seconds of same-titled completed
        steps for this agent × 1.3 buffer, else the caller's fallback. This is
        the "时间校准确定性" — first-run tasks use the fallback, repeats self-correct."""
        if fallback and fallback > 0:
            return fallback
        row = await self.session.execute(
            select(func.avg(TaskStep.actual_seconds))
            .join(Task, Task.id == TaskStep.task_id)
            .where(Task.agent_id == agent_id, TaskStep.title == title,
                   TaskStep.actual_seconds.is_not(None))
        )
        avg = row.scalar()
        return _clamp_wake(float(avg) * 1.3) if avg else 600

    # ------------------------------------------------------------------
    async def register(
        self, *, user_id: str, agent_id: str, title: str, goal: str,
        steps: list[dict[str, Any]], redline_tags: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        """Create a task + its ordered steps and schedule the first wake.

        The registering session is the one about to do step 0 right now; the
        first wake is the *safety net* that fires if it stalls before reporting.
        """
        if not steps:
            raise ValueError("a task needs at least one step")
        task = Task(
            user_id=user_id, agent_id=agent_id, title=title, goal=goal or "",
            status=TaskStatus.RUNNING.value, current_step=0,
            redline_tags=list(redline_tags or []), attempts_on_current=0,
        )
        self.session.add(task)
        await self.session.flush()  # assign task.id

        step_rows: list[TaskStep] = []
        for i, s in enumerate(steps):
            est = await self._calibrated_est(agent_id, s.get("title", ""), int(s.get("est_seconds", 0) or 0))
            row = TaskStep(
                task_id=task.id, idx=i, title=s.get("title", f"step {i}"),
                instruction=s.get("instruction", ""), verify_cmd=s.get("verify_cmd", ""),
                est_seconds=est, status=StepStatus.PENDING.value,
            )
            step_rows.append(row)
            self.session.add(row)

        step_rows[0].status = StepStatus.RUNNING.value
        task.next_wake_at = _now() + timedelta(seconds=step_rows[0].est_seconds)
        await self.session.flush()
        return _task_to_dict(task, step_rows)

    async def get(self, task_id: str) -> Optional[dict[str, Any]]:
        task = await self.session.get(Task, task_id)
        if task is None:
            return None
        return _task_to_dict(task, await self._steps(task_id))

    async def list_due(self, agent_id: Optional[str] = None, limit: int = 20) -> list[dict[str, Any]]:
        """Tasks whose wake is due — the per-runtime cron polls this, then spawns
        a session per row using :meth:`wake_payload`."""
        stmt = select(Task).where(
            Task.status == TaskStatus.RUNNING.value,
            Task.next_wake_at.is_not(None),
            Task.next_wake_at <= _now(),
        )
        if agent_id:
            stmt = stmt.where(Task.agent_id == agent_id)
        stmt = stmt.order_by(Task.next_wake_at).limit(limit)
        rows = (await self.session.execute(stmt)).scalars().all()
        return [_task_to_dict(t) for t in rows]

    # ------------------------------------------------------------------
    async def wake_payload(self, task_id: str) -> dict[str, Any]:
        """Render what a freshly-woken session should do for the current step,
        enforcing the attempt cap and the redline escalation.

        Each wake counts as one attempt to push the current step. Over the cap →
        the task is BLOCKED and an escalation payload is returned (the cron driver
        sends it to the human via Feishu/微信 instead of spawning a work session).
        A redline on the current step blocks the same way.
        """
        task = await self.session.get(Task, task_id)
        if task is None:
            return {"error": f"task not found: {task_id}"}
        if task.status != TaskStatus.RUNNING.value:
            return {"task_id": task_id, "status": task.status, "action": "none",
                    "reason": f"task is {task.status}, not running"}

        steps = await self._steps(task_id)
        if task.current_step >= len(steps):
            task.status = TaskStatus.DONE.value
            task.next_wake_at = None
            return {"task_id": task_id, "status": task.status, "action": "none",
                    "reason": "no more steps"}
        step = steps[task.current_step]

        # Redline gate — never auto-cross it unattended.
        hit = self._redline_hit(task, step.instruction)
        if hit:
            return await self._block(task, step, reason=f"redline: {hit}", action="escalate")

        # Attempt cap.
        task.attempts_on_current += 1
        step.attempts = task.attempts_on_current
        task.last_heartbeat = _now()
        if task.attempts_on_current > MAX_ATTEMPTS:
            return await self._block(
                task, step,
                reason=f"step '{step.title}' still unfinished after {MAX_ATTEMPTS} wakes",
                action="escalate",
            )

        # Lease: push the next wake out by this step's estimate so the task is
        # NOT re-listed as due while the just-woken session is still working /
        # before it reports. Without this, every cron tick re-wakes the same
        # task (next_wake_at is otherwise only moved by report()), double-spawning
        # sessions and burning the attempt cap in MAX_ATTEMPTS ticks. If the
        # woken session dies without reporting, the lease expires and it is
        # re-woken — the safety net still fires, just not on every single tick.
        task.next_wake_at = _now() + timedelta(seconds=_clamp_wake(step.est_seconds))
        await self.session.flush()
        prompt = (
            f"【无人值守·继续长任务】任务:{task.title}\n"
            f"总目标:{task.goal}\n"
            f"当前第 {step.idx + 1}/{len(steps)} 步:{step.title}\n"
            f"要做:{step.instruction}\n"
            f"先跑这条命令确定当前真实进度(不要凭记忆猜):\n    {step.verify_cmd or '(无 verify_cmd,按产物自行判断)'}\n"
            f"- 若已完成:调用 task_report(task_id, step_idx={step.idx}, verified_done=true, actual_seconds=...)\n"
            f"- 若未完成:继续把这一步做下去,做完再 verify、再 report;这是无人值守模式,自行判断,不要停下来等我回话。\n"
            f"- 只有遇到必须主人决断的事(删除/花钱/对外/家庭域)才停下升级。\n"
            f"(第 {task.attempts_on_current}/{MAX_ATTEMPTS} 次唤醒)"
        )
        return {
            "task_id": task_id, "status": task.status, "action": "work",
            "step_idx": step.idx, "title": step.title, "instruction": step.instruction,
            "verify_cmd": step.verify_cmd, "goal": task.goal,
            "attempt": task.attempts_on_current, "max_attempts": MAX_ATTEMPTS,
            "unattended": True, "prompt": prompt,
        }

    async def _block(self, task: Task, step: TaskStep, *, reason: str, action: str) -> dict[str, Any]:
        task.status = TaskStatus.BLOCKED.value
        task.next_wake_at = None
        task.note = reason
        await self.session.flush()
        return {
            "task_id": task.id, "status": task.status, "action": action,
            "step_idx": step.idx, "title": step.title, "reason": reason,
            "escalate_to_human": True,
            "prompt": (
                f"【任务受阻·需主人决断】任务:{task.title}\n"
                f"卡在第 {step.idx + 1} 步「{step.title}」:{reason}\n"
                f"通过飞书/微信问主人怎么处理。"
            ),
        }

    # ------------------------------------------------------------------
    async def report(
        self, *, task_id: str, step_idx: int, verified_done: bool,
        actual_seconds: Optional[int] = None, note: str = "",
    ) -> dict[str, Any]:
        """A woken session reports the deterministic result of the current step."""
        task = await self.session.get(Task, task_id)
        if task is None:
            return {"error": f"task not found: {task_id}"}
        if task.status not in (TaskStatus.RUNNING.value,):
            return {"task_id": task_id, "status": task.status, "action": "none",
                    "reason": f"task is {task.status}"}

        steps = await self._steps(task_id)
        if step_idx != task.current_step:
            return {"error": f"step_idx {step_idx} is not the current step ({task.current_step})",
                    "current_step": task.current_step}
        step = steps[task.current_step]
        if note:
            task.note = note

        if not verified_done:
            # Re-estimate and reschedule; the attempt was already counted at wake.
            next_secs = _clamp_wake(step.est_seconds * WAKE_BACKOFF)
            task.next_wake_at = _now() + timedelta(seconds=next_secs)
            await self.session.flush()
            return {"task_id": task_id, "status": task.status, "action": "reschedule",
                    "step_idx": step.idx, "next_wake_seconds": next_secs,
                    "next_wake_at": task.next_wake_at.isoformat()}

        # Verified done → close the step, advance.
        step.status = StepStatus.DONE.value
        if actual_seconds is not None:
            step.actual_seconds = int(actual_seconds)
        task.attempts_on_current = 0
        task.current_step += 1
        if task.current_step >= len(steps):
            task.status = TaskStatus.DONE.value
            task.next_wake_at = None
            await self.session.flush()
            return {"task_id": task_id, "status": task.status, "action": "done",
                    "reason": "all steps complete"}
        nxt = steps[task.current_step]
        nxt.status = StepStatus.RUNNING.value
        task.next_wake_at = _now() + timedelta(seconds=_clamp_wake(nxt.est_seconds))
        await self.session.flush()
        return {"task_id": task_id, "status": task.status, "action": "next_step",
                "step_idx": nxt.idx, "title": nxt.title, "instruction": nxt.instruction,
                "verify_cmd": nxt.verify_cmd, "next_wake_at": task.next_wake_at.isoformat()}

    async def cancel(self, task_id: str) -> bool:
        task = await self.session.get(Task, task_id)
        if task is None:
            return False
        task.status = TaskStatus.CANCELLED.value
        task.next_wake_at = None
        await self.session.flush()
        return True

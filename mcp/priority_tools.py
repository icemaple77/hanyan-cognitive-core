"""MCP tools for Priority Compass (公子的价值坐标 registry)。

Thin async wrappers over :class:`gateway.services.priority_service.PriorityService`,
same in-process pattern as :mod:`mcp.task_tools`. 会话内 agent 用这些工具:

    聊天路(§六):公子说「最近 X 最要紧」→ agent priority_set(source="agent:<name>")
    默认落 pending(半权隔离生效)→ 早安复核 → 公子一句话 priority_confirm 转正。

价值读时算:登记后无需回刷,context_builder 下一次读路自动 join(60s 缓存)。
"""
from __future__ import annotations

from typing import Any, Optional

from gateway.core.database import async_session
from gateway.services.priority_service import PriorityService

__all__ = ["priority_set", "priority_list", "priority_confirm", "priority_retire"]


def _ok(**payload: Any) -> dict[str, Any]:
    return {"success": True, **payload}


def _err(message: str) -> dict[str, Any]:
    return {"success": False, "error": message}


async def priority_set(
    label: str,
    importance: int = 3,
    urgency: int = 3,
    anchors: Optional[list[str]] = None,
    source: str = "agent",
    trust: Optional[str] = None,
    review_at: Optional[str] = None,
    user_id: str = "michael",
) -> dict[str, Any]:
    """登记一条价值坐标(重要性×紧急性,各 1-5)。

    agent 提案默认落 pending(半权隔离生效,不污染全局、也压不动紧急事),需公子
    priority_confirm 转正;source="gongzi" 则直接 confirmed。anchors 是主题锚词
    (如 ["肩颈","养伤","复诊"]),读路 join 命中即加成。review_at 为 ISO 日期,
    过期 7 天未复核 α 自动减半。
    """
    try:
        async with async_session() as session:
            svc = PriorityService(session)
            p = await svc.set(
                label=label, importance=importance, urgency=urgency,
                anchors=anchors, source=source, trust=trust,
                review_at=review_at, user_id=user_id,
            )
            await session.commit()
            return _ok(priority=p)
    except ValueError as e:
        return _err(str(e))
    except Exception as e:
        return _err(f"{type(e).__name__}: {e}")


async def priority_list(user_id: str = "michael", status: Optional[str] = "active") -> dict[str, Any]:
    """列出价值坐标(默认只列 active,带象限/α)。status=None 列全部(含退役版本链)。"""
    try:
        async with async_session() as session:
            svc = PriorityService(session)
            return _ok(priorities=await svc.list(user_id=user_id, status=status))
    except Exception as e:
        return _err(f"{type(e).__name__}: {e}")


async def priority_confirm(priority_id: str) -> dict[str, Any]:
    """把 pending 提案转正为 confirmed(全权重)。公子的一句"转正"走这。"""
    try:
        async with async_session() as session:
            svc = PriorityService(session)
            p = await svc.confirm(priority_id)
            if p is None:
                return _err("priority not found")
            await session.commit()
            return _ok(priority=p)
    except Exception as e:
        return _err(f"{type(e).__name__}: {e}")


async def priority_retire(priority_id: str, superseded_by: Optional[str] = None) -> dict[str, Any]:
    """退役一条(不物删,记忆铁律)。superseded_by 给出则记版本链(新事实取代旧优先级)。"""
    try:
        async with async_session() as session:
            svc = PriorityService(session)
            p = await svc.retire(priority_id, superseded_by=superseded_by)
            if p is None:
                return _err("priority not found")
            await session.commit()
            return _ok(priority=p)
    except Exception as e:
        return _err(f"{type(e).__name__}: {e}")

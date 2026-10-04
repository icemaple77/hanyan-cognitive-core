"""agent 范围:私有命名空间(专家 agent 的个人库)。

agent_id 以 ``expert:`` 开头的记忆只有指名该 agent_id 才读得到;不限 agent 的检索、
做梦巩固、去重清扫一律看不到它们——专家的工作笔记不该被揉进含烟的记忆。
"""
from sqlalchemy import func

from gateway.models import Memory

PRIVATE_AGENT_PREFIX = "expert:"


def scope_agent(stmt, agent_id=None):
    """给查询加 agent 范围:指名则只看那个 agent;不指名则排除私有命名空间。"""
    if agent_id:
        return stmt.where(Memory.agent_id == agent_id)
    return stmt.where(func.coalesce(Memory.agent_id, "").notlike(PRIVATE_AGENT_PREFIX + "%"))

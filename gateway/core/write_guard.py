"""写入侧预防：入库前的精确查重。

背景（2026-09-29 实测）：
    库里重复的来源不是检索，是**写入**——会话原文原样入库、dream 空壳摘要。
    ``gateway/core/dedupe.py`` 是「事后清扫」（每晚），这里补「事前拦截」，
    否则就是边漏边拖。

规则（受开关控制、**默认关闭**，不启用就与现状逐字节一致）：
     ``store_dedupe_mode == "skip"`` 时，窗口内已有逐字同文 → 跳过插入、
    返回既有那条（顺带省一次嵌入计算）。

设计要点：
- 查重限定 ``created_at`` 窗口，让每次写入的查询**有界**；更老的重复交给
  ``prune_exact_duplicates`` 每晚清扫。
- **不按 agent 分域**——实测的重复恰恰是跨 agent 的同一段原文
  （claude-code 与 openclaw 各存一份），分域会漏。

已废弃：``tool_result`` 截断（``store_tool_result_max_chars``）。
    2026-09-29 公子拍板：tool_result **源头不落库**（见 ``hcc-openclaw-plugin``
    的 ``tool_result_persist`` 处理器）。源头关了就别再截断——两者是替代关系，
    留截断就是死代码。
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from core.config import core_settings
from gateway.models import Memory

logger = logging.getLogger(__name__)


async def find_exact_duplicate(session, *, content: str, window_hours: int) -> Memory | None:
    """窗口内是否已有逐字相同的 active 记忆；未启用查重时恒返回 None。"""
    if core_settings.store_dedupe_mode != "skip" or not content:
        return None
    since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=max(1, int(window_hours)))
    row = (
        await session.execute(
            select(Memory)
            .where(Memory.status == "active")
            .where(Memory.content == content)
            .where(Memory.created_at >= since)
            .limit(1)
        )
    ).scalars().first()
    if row is not None:
        logger.info("store dedupe: exact duplicate within %sh → memory %s", window_hours, row.id)
    return row


# 系统自己产生、不该当记忆检索的记录(2026-10-04 盘点,存量已由
# scripts/purge_pollution.py 软删)。入库时直接标 discarded:留痕可审计,但不进检索。
_SYSTEM_NOISE_RE = re.compile(
    r"^(?:\[OpenClaw session_end\]"
    r"|user: \[(?:Inter-session message|Subagent Context)"
    r"|User: \[IMPORTANT: Background process)"
)
SYSTEM_NOISE_TAG = "system_noise"


def is_system_noise(content: str | None) -> bool:
    """这条内容是不是系统通知/跨会话转发这类噪音。"""
    return bool(content) and _SYSTEM_NOISE_RE.match(content) is not None

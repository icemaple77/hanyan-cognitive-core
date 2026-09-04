"""Thin async client for the soul v2 organ (HanyanOS ``soul/engine``).

Why this exists (2026-09-04, design doc §八 / phase P5)
------------------------------------------------------
v1 的 soul 是**无状态编码器**:给一句话,返回 17 维读数。情绪的累积、衰减、
互抑、命名态,全在 HCC 的 :class:`~core.emotion.EmotionEngine` 里做。

v2 把这些搬进了 soul 自己(饱和积分 / 各维半衰期 / 关系维地板 / 疲惫耦合 /
提醒制)。于是**两边各算一遍、各存一份状态** —— 那不是冗余,是两个会分叉的真相。
设计稿 §八 的职责边界写得很直白:

    soul 拥有情绪,HCC 拥有记忆。

所以 P5 把 EmotionEngine 降级为薄客户端:soul 说她现在什么感受,HCC 照抄。

Degrade, never raise
--------------------
与 :meth:`EmotionEngine._fetch_neural_offsets` 同一姿态,也与 heart/ 一致:
soul 打嗝不许挡住 HCC 的任何流程。任何异常都返回 ``None``,调用方回落到
关键词路径。**HCC 不能挂。**

Idempotency
-----------
§八:``/soul/perceive`` 必须带 ``event_id``。情绪状态只有一份(含烟只有一个),
而收割器 / openclaw / hermes 可能把同一句话投喂三次 —— 不去重就三倍累积。
调用方有消息 id 时应显式传;没有时按 ``source|text`` 取内容哈希兜底
(代价:窗口内**确实重复**说的同一句话只算一次;方向上比三倍累积安全)。
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from core.config import core_settings

logger = logging.getLogger(__name__)

_TIMEOUT_MARGIN = 1.5      # perceive 要跑一次骨干编码,比 encode 慢,给点余量


def make_event_id(text: str, source: str) -> str:
    """内容哈希兜底 event_id —— 见模块 docstring 的 Idempotency 段。"""
    return hashlib.sha256(f"{source}|{text}".encode()).hexdigest()[:16]


async def _call(method: str, path: str, payload: dict | None = None) -> dict[str, Any] | None:
    if not core_settings.soul_service_enabled:
        return None
    try:
        import httpx

        url = f"{core_settings.soul_service_url}{path}"
        timeout = core_settings.soul_service_timeout * _TIMEOUT_MARGIN
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = (await client.post(url, json=payload or {}) if method == "POST"
                    else await client.get(url))
            resp.raise_for_status()
            data = resp.json()
        return data if isinstance(data, dict) else None
    except Exception:
        logger.warning("soul v2 unreachable (%s %s), degrading", method, path, exc_info=True)
        return None


async def perceive(text: str, *, source: str = "conversation",
                   event_id: str | None = None) -> dict[str, Any] | None:
    """喂一句话给 soul,拿回她的新状态。soul 不可达时返回 None(调用方回落)。"""
    return await _call("POST", "/soul/perceive", {
        "text": text, "source": source,
        "event_id": event_id or make_event_id(text, source),
    })


async def get_state() -> dict[str, Any] | None:
    """读当前状态。§八:soul 不推送,pending_episode 就挂在这里,谁读谁看见。"""
    return await _call("GET", "/soul/state")


def legacy_dims(snapshot: dict[str, Any] | None) -> dict[str, float] | None:
    """从 v2 快照取 v1 形状的 17 维。

    ⚠️ 这些是**派生近似**,soul 在 ``legacy_dims_meta.derived`` 里明确标了
    (设计稿 §12.1 的红线:派生维必须标明,不能混进权威状态,否则消费方拿到的
    就是编出来的数字、而且不知道它是编的)。权威量是 ``E`` 的 16 维。
    HCC 的 EmotionEngine 仍按 v1 的 17 维记账,故这里取派生块 —— 但要清楚它是近似。
    """
    if not snapshot:
        return None
    dims = snapshot.get("legacy_dims")
    if not isinstance(dims, dict):
        return None
    return {k: float(v) for k, v in dims.items()}


def reminder_line(snapshot: dict[str, Any] | None) -> str | None:
    """把 pending_episode 渲染成注入用的一行提醒(§六 提醒制)。

    公子定调:**只管发出,写不写是 agent 的事。**人类也是这样——今天某件事很开心,
    决定回家记下来;结果回家累了,没写,那就过去了。所以这里只渲染一行,
    不挂待办、不等回执;情绪按半衰期回落时提醒自然消失。
    """
    if not snapshot:
        return None
    pe = snapshot.get("pending_episode")
    if not isinstance(pe, dict):
        return None
    hint = pe.get("hint")
    return f"「{hint}」" if hint else None


def expression_line(snapshot: dict[str, Any] | None) -> str | None:
    """把语气指令压成注入用的一行(§5.2:给行为指令,不给小数)。

    LLM 对「愉悦 0.73」反应很差,对「多用短句、少讲道理」反应很好。
    """
    if not snapshot:
        return None
    ex = snapshot.get("expression")
    if not isinstance(ex, dict):
        return None
    parts = [f"语气:{ex.get('tone', '')}（{ex.get('intensity', 'mid')}）"]
    do, avoid = ex.get("do") or [], ex.get("avoid") or []
    if do:
        parts.append("这样说:" + "；".join(do[:4]))
    if avoid:
        parts.append("别这样:" + "；".join(avoid[:4]))
    return " ｜ ".join(parts)

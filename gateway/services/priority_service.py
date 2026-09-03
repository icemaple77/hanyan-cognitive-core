"""Priority Compass service —— 公子的『价值坐标』读写 + 象限→α 派生。

设计见 docs/priority-compass-design.md。本服务只管 registry 的增删查改与"有效
权重"的读时派生;它**从不修改 memories**——价值读时算,绝不落盘。context_builder
读路调 :func:`active_for_read` 拿到一份 {label, anchors, alpha} 清单做 join。

α 派生(§五.2 + Claude Code 会诊裁定):
- 象限:Q1(imp≥4∧urg≥4)=0.5 · Q2(imp≥4)=0.25 · Q3(urg≥4)=0 · Q4=0。
  **Q3 记忆轴 α=0 不设负**——压制拖延放行为轴(work-driver 派活),记忆轴设负=让真相
  因"不重要"而不被召回,那是记忆撒谎。
- 门槛 B:trust=pending → α 减半(压不动紧急、也不污染全局,确认后转正)。
- 防腐:review_at 过期 > 7 天未复核 → α 再减半,registry 不烂尾。
"""
from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.models import Priority, PriorityStatus, PriorityTrust

__all__ = ["PriorityService", "quadrant_of", "quadrant_alpha"]

_REVIEW_GRACE_DAYS = 7  # review_at 过期多久后 α 减半


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def quadrant_of(importance: int, urgency: int) -> str:
    """艾森豪威尔四象限(不落列,读时派生)。"""
    if importance >= 4 and urgency >= 4:
        return "Q1"  # 重要且紧急
    if importance >= 4:
        return "Q2"  # 重要不紧急
    if urgency >= 4:
        return "Q3"  # 紧急不重要
    return "Q4"      # 不重要不紧急


def quadrant_alpha(importance: int, urgency: int, *, trust: str, review_at: Optional[date]) -> float:
    """一条 priority 对记忆轴的加权系数 α(≥0,记忆轴永不为负)。"""
    base = {"Q1": 0.5, "Q2": 0.25, "Q3": 0.0, "Q4": 0.0}[quadrant_of(importance, urgency)]
    if base == 0.0:
        return 0.0
    if trust == PriorityTrust.PENDING.value:
        base *= 0.5  # 门槛 B:隔离生效,半权
    if review_at is not None and date.today() > review_at + timedelta(days=_REVIEW_GRACE_DAYS):
        base *= 0.5  # 防腐:过期未复核,减半
    return round(base, 4)


# 读路 60s 缓存:active 行 ≤ 10,但每个 /context 都读一次划不来。改任何一行会
# 走写路(set/confirm/retire),那里主动清缓存,所以缓存不会脏。
_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_CACHE_TTL = 60.0


def _invalidate_cache(user_id: Optional[str] = None) -> None:
    if user_id is None:
        _CACHE.clear()
    else:
        _CACHE.pop(user_id, None)


class PriorityService:
    def __init__(self, session: AsyncSession):
        self.session = session

    @staticmethod
    def _to_dict(p: Priority) -> dict[str, Any]:
        return {
            "id": p.id,
            "user_id": p.user_id,
            "label": p.label,
            "anchors": p.anchors or [],
            "importance": p.importance,
            "urgency": p.urgency,
            "quadrant": quadrant_of(p.importance, p.urgency),
            "source": p.source,
            "trust": p.trust,
            "status": p.status,
            "review_at": p.review_at.isoformat() if p.review_at else None,
            "superseded_by": p.superseded_by,
            "alpha": quadrant_alpha(p.importance, p.urgency, trust=p.trust, review_at=p.review_at),
            "created_at": p.created_at.isoformat() if p.created_at else None,
            "updated_at": p.updated_at.isoformat() if p.updated_at else None,
        }

    # ── 写路 ──────────────────────────────────────────────────────────
    async def set(
        self,
        *,
        label: str,
        importance: int = 3,
        urgency: int = 3,
        anchors: Optional[list[str]] = None,
        source: str = "gongzi",
        trust: Optional[str] = None,
        review_at: Optional[str] = None,
        user_id: str = "michael",
    ) -> dict[str, Any]:
        """登记一条价值坐标。source=gongzi 默认直接 confirmed;agent 提案默认 pending。"""
        if not label or not label.strip():
            raise ValueError("label must not be empty")
        importance = max(1, min(5, int(importance)))
        urgency = max(1, min(5, int(urgency)))
        if trust is None:
            trust = PriorityTrust.CONFIRMED.value if source == "gongzi" else PriorityTrust.PENDING.value
        rev: Optional[date] = None
        if review_at:
            rev = date.fromisoformat(review_at)
        # embedding 列暂留空(向前兼容):P1 读路走 anchor 命中,不用 emb-join。
        # 不在写路预先算向量——那是次会失败、又没人用的 ollama 调用。等真做 emb
        # 余弦 join(需检索层暴露记忆向量)时再回填,见 docs/priority-compass-design.md。
        p = Priority(
            user_id=user_id, label=label.strip(), anchors=anchors or [],
            importance=importance, urgency=urgency, source=source, trust=trust,
            status=PriorityStatus.ACTIVE.value, review_at=rev, embedding=None,
        )
        self.session.add(p)
        await self.session.flush()
        _invalidate_cache(user_id)
        return self._to_dict(p)

    async def confirm(self, priority_id: str) -> Optional[dict[str, Any]]:
        """公子把 pending 提案转正 → confirmed(全权重)。"""
        p = await self.session.get(Priority, priority_id)
        if p is None:
            return None
        p.trust = PriorityTrust.CONFIRMED.value
        await self.session.flush()
        _invalidate_cache(p.user_id)
        return self._to_dict(p)

    async def retire(self, priority_id: str, *, superseded_by: Optional[str] = None) -> Optional[dict[str, Any]]:
        """退役一条(不物删,记忆铁律):superseded_by 给出则记版本链。"""
        p = await self.session.get(Priority, priority_id)
        if p is None:
            return None
        p.status = PriorityStatus.SUPERSEDED.value if superseded_by else PriorityStatus.EXPIRED.value
        if superseded_by:
            p.superseded_by = superseded_by
        await self.session.flush()
        _invalidate_cache(p.user_id)
        return self._to_dict(p)

    # ── 读路 ──────────────────────────────────────────────────────────
    async def list(self, *, user_id: str = "michael", status: Optional[str] = "active") -> list[dict[str, Any]]:
        stmt = select(Priority).where(Priority.user_id == user_id)
        if status:
            stmt = stmt.where(Priority.status == status)
        stmt = stmt.order_by(Priority.importance.desc(), Priority.urgency.desc())
        rows = await self.session.execute(stmt)
        return [self._to_dict(p) for p in rows.scalars().all()]

    async def active_for_read(self, *, user_id: str = "michael") -> list[dict[str, Any]]:
        """context_builder 读路专用:active 且 α>0 的条目(带 anchors/alpha),60s 缓存。"""
        hit = _CACHE.get(user_id)
        if hit and (time.monotonic() - hit[0]) < _CACHE_TTL:
            return hit[1]
        rows = await self.list(user_id=user_id, status=PriorityStatus.ACTIVE.value)
        active = [r for r in rows if r["alpha"] > 0]
        _CACHE[user_id] = (time.monotonic(), active)
        return active

"""精确内容去重：逐字相同（md5）的 active 记忆只留一份，其余软删。

为什么需要（2026-09-29 实测）：
- 全库 active 里按 content 精确比对有 969 组重复 / 1378 份冗余副本；
- **dream 自己也在产出重复**：42 组 / 123 份逐字相同，且昨夜仍在新增。
  根因见 ``core/dream.py`` 里 ``_group_for_knowledge`` 的注释——簇的 key 用成员
  id 集合哈希，而成员集每晚必变，于是同一话题每晚新建一条、旧的还留着。
- dream 现有的 ``_dedupe_by_embedding``（阈值 0.9）**只用于决定要不要加 light
  信号，从不删/tag 行**，所以行级重复它管不到。

本模块是该缺口的补齐：可被 dream 的 light 阶段每晚调用，让重复不再累积。

规矩（与 scripts/dedupe_by_content.py / dedupe_dream_knowledge.py 一致）：
- **只软删**（``status='discarded'`` + 标签），永不物理删除
- 每组保留「代表」：importance 降序 → access_count 降序 → created_at 升序
- 组内出现 ``PROTECTED_TAGS``（回忆录/永久承诺…）→ **整组跳过**
- ``dry_run=True`` 时只试算不写库
"""

from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from datetime import datetime

from sqlalchemy import String, cast, func, select

from core.forget import PROTECTED_TAGS
from gateway.models import Memory, MemoryStatus

logger = logging.getLogger(__name__)

DISCARD_TAG = "dedupe:content-auto"
#: 只处理长度 >= 该值的内容。短句的逐字重复往往是「真的说了两次」，折叠会丢事件计数。
DEFAULT_MIN_LEN = 120

# --- 聊天室 session 归并 ---------------------------------------------------
CHATROOM_TAG = "agent-chatroom"
CHATROOM_DISCARD_TAG = "dedupe:chatroom-session"
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def _tags_of(m: Memory) -> set[str]:
    raw = m.tags
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            raw = []
    return {str(t) for t in (raw or [])}


def _rank(m: Memory):
    return (
        -(m.importance or 0.0),
        -(m.access_count or 0),
        m.created_at or datetime.min,
    )


async def prune_exact_duplicates(
    session,
    *,
    min_len: int = DEFAULT_MIN_LEN,
    source_like: str | None = None,
    dry_run: bool = False,
) -> dict:
    """把逐字相同的 active 记忆折成一份（软删其余）。

    ``source_like`` 非空时只处理 ``source LIKE '%<值>%'`` 的行——用于给某一类
    来源单独放低门槛（例：dream 的巩固空壳只有 36–37 字，但同样是逐字重复，
    而全库降门槛会把人类对话的短重复一并扫掉）。

    返回 ``{"groups", "discarded", "skipped_protected", "by_source"}``。
    调用方负责 ``session.commit()``（dry_run 不写）。
    """
    q = (
        select(Memory)
        .where(Memory.status == MemoryStatus.ACTIVE)
        .where(func.length(Memory.content) >= min_len)
    )
    if source_like:
        q = q.where(Memory.source.like(f"%{source_like}%"))
    rows = (await session.execute(q)).scalars().all()

    groups: dict[str, list[Memory]] = defaultdict(list)
    for m in rows:
        groups[str(m.content or "")].append(m)

    dup_groups = {k: v for k, v in groups.items() if len(v) > 1}

    to_discard: list[Memory] = []
    skipped = 0
    for members in dup_groups.values():
        if any(_tags_of(m) & PROTECTED_TAGS for m in members):
            skipped += 1
            continue
        ordered = sorted(members, key=_rank)
        to_discard.extend(ordered[1:])

    by_source: dict[str, int] = defaultdict(int)
    for m in to_discard:
        by_source[m.source or "(none)"] += 1

    if not dry_run:
        for m in to_discard:
            m.status = MemoryStatus.DISCARDED
            tags = _tags_of(m)
            tags.add(DISCARD_TAG)
            m.tags = sorted(tags)

    stats = {
        "groups": len(dup_groups),
        "discarded": len(to_discard),
        "skipped_protected": skipped,
        "by_source": dict(sorted(by_source.items(), key=lambda x: -x[1])),
        "dry_run": dry_run,
    }
    logger.info("prune_exact_duplicates: %s", {k: v for k, v in stats.items() if k != "by_source"})
    return stats


def _session_of(m: Memory) -> str | None:
    hit = UUID_RE.search(json.dumps(sorted(_tags_of(m)), ensure_ascii=False))
    return hit.group(0) if hit else None


async def prune_chatroom_sessions(session, *, dry_run: bool = False) -> dict:
    """同一场 agent-chatroom 被多 agent 重录多份时，每场只留一份（软删其余）。

    这些是**近似重复**（同一场对话的不同视角），逐字不同，md5 精确去重抓不到；
    可行口径是按 tags 里的 session UUID 归并（2026-09-29 实测 25 → 4）。
    每场保留**内容最长**那份（「完整对话记录」取最全的），平手再比
    importance → access_count → created_at。

    返回 ``{"groups", "discarded", "skipped_protected"}``。
    """
    rows = (
        await session.execute(
            select(Memory)
            .where(Memory.status == MemoryStatus.ACTIVE)
            .where(cast(Memory.tags, String).like(f"%{CHATROOM_TAG}%"))
        )
    ).scalars().all()

    groups: dict[str, list[Memory]] = defaultdict(list)
    for m in rows:
        sid = _session_of(m)
        if sid:
            groups[sid].append(m)

    dup_groups = {k: v for k, v in groups.items() if len(v) > 1}
    to_discard: list[Memory] = []
    skipped = 0
    for members in dup_groups.values():
        if any(_tags_of(m) & PROTECTED_TAGS for m in members):
            skipped += 1
            continue
        ordered = sorted(
            members,
            key=lambda m: (
                -len(m.content or ""),
                -(m.importance or 0.0),
                -(m.access_count or 0),
                m.created_at or datetime.min,
            ),
        )
        to_discard.extend(ordered[1:])

    if not dry_run:
        for m in to_discard:
            m.status = MemoryStatus.DISCARDED
            tags = _tags_of(m)
            tags.add(CHATROOM_DISCARD_TAG)
            m.tags = sorted(tags)

    stats = {
        "groups": len(dup_groups),
        "discarded": len(to_discard),
        "skipped_protected": skipped,
        "discarded_ids": [m.id for m in to_discard],
        "dry_run": dry_run,
    }
    logger.info(
        "prune_chatroom_sessions: %s",
        {k: v for k, v in stats.items() if k != "discarded_ids"},
    )
    return stats

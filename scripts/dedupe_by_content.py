#!/usr/bin/env python3
"""按内容查重：同一段原文在多处重复入库时，只留一份，其余软删。

背景（2026-09-29 实测）：
    active 记忆里，**按 content 精确比对**有 969 组重复、**1378 份冗余副本**；
    最大一组是同一段 8000 字原文的 50+ 份（跨 claude-code / openclaw 多个 agent），
    最长 200 条里还有 2173 对语义相似度 ≥0.95。
    存储只浪费 1.9 MB（不心疼），**真正贵的是检索名额**——每份副本都去抢 top-k。

设计（与 scripts/dedupe_dream_knowledge.py / dedupe_kanban_memories.py 同规矩）：
- **默认 dry-run**，只有 `--apply` 才写
- 只**软删**（`status='discarded'` + 打标签 `dedup_content_<日期>`），**永不物理删除**
- 每组保留「代表」：importance 降序 → access_count 降序 → created_at 升序（最早的）
- **保护记忆整组跳过**：组内出现 forget.PROTECTED_TAGS（回忆录/永久承诺…）时不动
- 只处理**长内容**（默认 ≥200 字）：短句重复往往是「真的说了两次」，折叠会丢掉事件计数
- 写前落一份快照 JSON（被改动的 id 列表），可据此一键回滚
- 建议先跑 `scripts/backup_hcc.sh`（或等效 pg_dump）

用法：
    env -u PYTHONPATH python scripts/dedupe_by_content.py --dry-run
    env -u PYTHONPATH python scripts/dedupe_by_content.py --apply
    env -u PYTHONPATH python scripts/dedupe_by_content.py --apply --min-len 500
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select, text  # noqa: E402

from core.forget import PROTECTED_TAGS  # noqa: E402
from gateway.core.database import async_session  # noqa: E402
from gateway.models import Memory  # noqa: E402

TAG_PREFIX = "dedup_content"
SNAPSHOT_DIR = Path.home() / "backups" / "hcc"


def _tags_of(m: Memory) -> set[str]:
    raw = m.tags
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            raw = []
    return {str(t) for t in (raw or [])}


def _rank(m: Memory):
    """代表排序：importance 高优先，其次被访问多，最后取更早创建的。"""
    return (-(m.importance or 0.0), -(m.access_count or 0), m.created_at or datetime.min)


async def main() -> None:
    ap = argparse.ArgumentParser(description="按内容查重（只软删，默认 dry-run）")
    ap.add_argument("--apply", action="store_true", help="真正写库；不加则只试算")
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="只试算（与其它脚本习惯对齐；本就是默认行为）",
    )
    ap.add_argument("--min-len", type=int, default=200, help="只处理长度 >= 该值的内容（默认 200）")
    ap.add_argument("--limit-groups", type=int, default=0, help="只处理前 N 组（0=全部，调试用）")
    args = ap.parse_args()

    async with async_session() as session:
        rows = (
            await session.execute(
                select(Memory).where(Memory.status == "active").order_by(Memory.created_at.asc())
            )
        ).scalars().all()

    groups: dict[str, list[Memory]] = defaultdict(list)
    for m in rows:
        c = m.content or ""
        if len(c) < args.min_len:
            continue
        groups[str(hash(c))].append(m)

    dup_groups = {k: v for k, v in groups.items() if len(v) > 1}
    # 组内容以第一条为准（hash 相同即内容相同）
    dup_groups = {k: v for k, v in dup_groups.items() if len(set(x.content for x in v)) == 1}

    to_discard: list[Memory] = []
    skipped_protected = 0
    for members in dup_groups.values():
        if any(_tags_of(m) & PROTECTED_TAGS for m in members):
            skipped_protected += 1
            continue
        members_sorted = sorted(members, key=_rank)
        keep, rest = members_sorted[0], members_sorted[1:]
        del keep
        to_discard.extend(rest)

    total_rows = len(to_discard)
    by_source: dict[str, int] = defaultdict(int)
    for m in to_discard:
        by_source[m.source or "(none)"] += 1

    print(f"扫描：active {len(rows)} 条（长度 >= {args.min_len} 的按内容分组）")
    print(f"重复组：{len(dup_groups)} 组，可省副本：{total_rows} 份")
    print(f"跳过（含受保护记忆的组）：{skipped_protected} 组")
    print("按来源：", dict(sorted(by_source.items(), key=lambda x: -x[1])[:8]))

    if args.limit_groups:
        to_discard = to_discard[: args.limit_groups]
        print(f"（--limit-groups 生效，本次只处理 {len(to_discard)} 份）")

    if not args.apply or args.dry_run:
        print("\n[dry-run] 未写库。加 --apply 才执行。")
        for m in to_discard[:5]:
            print(f"  · 将软删 {m.id[:8]} ({m.source}, {len(m.content)} 字): {(m.content or '')[:40]!r}")
        return

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    tag = f"{TAG_PREFIX}_{stamp[:8]}"
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    snap = SNAPSHOT_DIR / f"dedupe_by_content_{stamp}.json"
    snap.write_text(
        json.dumps([{"id": m.id, "source": m.source, "len": len(m.content or "")} for m in to_discard],
                   ensure_ascii=False, indent=1),
        encoding="utf-8",
    )

    t0 = time.monotonic()
    async with async_session() as session:
        for m in to_discard:
            row = await session.get(Memory, m.id)
            if row is None:
                continue
            row.status = "discarded"
            tags = _tags_of(row)
            tags.add(tag)
            row.tags = sorted(tags)
        await session.commit()

    print(f"\n[apply] 软删 {len(to_discard)} 份，耗时 {time.monotonic()-t0:.1f}s，标签 {tag}")
    print(f"快照（回滚用）：{snap}")
    print("回滚：把快照里的 id 逐条 UPDATE status='active' 并移除该标签")


if __name__ == "__main__":
    asyncio.run(main())

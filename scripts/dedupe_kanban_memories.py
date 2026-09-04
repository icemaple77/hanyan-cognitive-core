#!/usr/bin/env python3
"""清理 kanban_sync 重复插入的记忆:每个 task_id 只留最新一条,其余标 discarded。

起因(2026-09-04,从 Obsidian 重复文件一路查上来):
  HanyanOS/memory/hcc_client.list_by_type 在任一页拉取失败时返回**已拿到的部分**
  (常常是空列表),kanban_sync.load_index 据此建索引 → 索引为空 →
  reconcile 认为 36 个看板任务全是新的 → **全量重插**。一小时一轮。
  结果:1224 条 kanban 记忆,唯一 task_id 只有 36 个;Obsidian 又忠实导出成
  758 个重复 md。写入端的洞已在那两个文件里堵上(失败改为整轮跳过),这里清存量。

**不硬删** —— 标 status=discarded,可回滚;并打标留痕。
留哪一条:每个 task_id 保留 created_at 最新的(它的 status 最接近看板现状)。
"""
import asyncio, json, re, sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sqlalchemy import select

from gateway.core.database import async_session
from gateway.models import Memory

SUMMARY_RE = re.compile(r"^\[kanban:(.+?)\|(.+?)\]$")
TAG = "dedup:kanban:2026-09-04"


async def main(apply: bool) -> None:
    async with async_session() as s:
        rows = (await s.execute(
            select(Memory).where(Memory.source == "kanban_sync").where(Memory.status == "active")
        )).scalars().all()
        by_task: dict[str, list] = defaultdict(list)
        unparsed = []
        for m in rows:
            mt = SUMMARY_RE.match((m.summary or "").strip())
            (by_task[mt.group(1)] if mt else unparsed).append(m) if mt else unparsed.append(m)
        print(f"active kanban 记忆 {len(rows)} 条 · 唯一 task_id {len(by_task)} 个 · summary 解析不了 {len(unparsed)} 条")
        drop = []
        for task_id, group in by_task.items():
            group.sort(key=lambda m: (m.created_at or datetime.min), reverse=True)
            drop.extend(group[1:])              # 留最新一条
        print(f"待标记 discarded: {len(drop)} 条(保留 {len(by_task)} 条)")
        snap = [{"id": str(m.id), "summary": m.summary, "created_at": str(m.created_at)} for m in drop]
        out = Path(__file__).parent / f"_kanban_dedup_{datetime.now(timezone.utc):%Y%m%d%H%M}.json"
        out.write_text(json.dumps(snap, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"原始清单已存盘 → {out.name}")
        if not apply:
            print("\n(dry-run;加 --apply 才真改)")
            return
        for m in drop:
            m.status = "discarded"
            m.tags = list({*(m.tags or []), TAG})
        await s.commit()
        print(f"\n已标记 {len(drop)} 条为 discarded,并打标 {TAG}")


if __name__ == "__main__":
    asyncio.run(main("--apply" in sys.argv))

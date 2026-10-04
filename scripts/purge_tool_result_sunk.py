#!/usr/bin/env python3
"""清理「只进不出」的 tool_result 沉没数据（2026-09-29 公子拍板 B）。

依据（active 实测）：
    2164 条 ``tool_result`` 里，**达到检索门槛（importance ≥ 0.5）的有 0 条**
    （全在 0.3–0.4），召回率 9.6%（conversation 是 47%）——日常检索根本捞不到，
    只有调用方显式指定 ``type="tool_result"`` 才可能返回。却持续占库容。

清理口径（**保守**，只清「从未被碰过」的）：
    type='tool_result' AND status='active' AND access_count = 0
    AND tags 不含 promoted / protected
→ 实测 1958 条 / 3.56 MB。保留：207 条曾被召回 + 10 条有人工痕迹。

规矩（沿用仓库既有清理脚本）：
- 默认 dry-run，``--apply`` 才写
- **只软删**（``status='discarded'`` + 标签 ``purge:tool_result-sunk``），**永不物理删除**
- 写前落 id 快照 JSON，可据此一键回滚
- 建议先跑 ``scripts/backup_hcc.sh``

配套（A，源头）：``hcc-openclaw-plugin`` 的 ``tool_result_persist`` 已默认停写。
只清存量不关源头 = 边漏边拖；只关源头不清存量 = 旧垃圾常驻。两件一起才闭环。

用法：
    env -u PYTHONPATH python scripts/purge_tool_result_sunk.py --dry-run
    env -u PYTHONPATH python scripts/purge_tool_result_sunk.py --apply
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import String, cast, select  # noqa: E402

from core.forget import PROTECTED_TAGS  # noqa: E402
from gateway.core.database import async_session  # noqa: E402
from gateway.models import Memory  # noqa: E402

PURGE_TAG = "purge:tool_result-sunk"
SNAPSHOT_DIR = Path.home() / "backups" / "hcc"


def _tags_of(m: Memory) -> list[str]:
    raw = m.tags
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            raw = []
    return [str(t) for t in (raw or [])]


async def main() -> None:
    ap = argparse.ArgumentParser(description="清理 tool_result 沉没数据（只软删，默认 dry-run）")
    ap.add_argument("--apply", action="store_true", help="真正写库；不加则只试算")
    ap.add_argument("--dry-run", action="store_true", help="只试算（本就是默认行为）")
    args = ap.parse_args()

    async with async_session() as session:
        rows = (
            await session.execute(
                select(Memory)
                .where(Memory.status == "active")
                .where(Memory.type == "tool_result")
                .where(Memory.access_count == 0)
            )
        ).scalars().all()

    targets = []
    for m in rows:
        if set(_tags_of(m)) & PROTECTED_TAGS:
            continue
        if "promoted" in " ".join(_tags_of(m)):
            continue
        targets.append(m)

    print(f"扫描：active 且 access_count=0 的 tool_result {len(rows)} 条")
    print(f"可清（无人工痕迹）：{len(targets)} 条，共 {sum(len(m.content or '') for m in targets)/1048576:.2f} MB")

    if not args.apply or args.dry_run:
        print("\n[dry-run] 未写库。加 --apply 才执行。")
        for m in targets[:3]:
            print(f"  · {m.id[:8]} ({len(m.content or '')} 字): {(m.content or '')[:40]!r}")
        return

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    snap = SNAPSHOT_DIR / f"purge_tool_result_sunk_{stamp}.json"
    snap.write_text(
        json.dumps([{"id": m.id, "len": len(m.content or "")} for m in targets], ensure_ascii=False, indent=1),
        encoding="utf-8",
    )

    t0 = time.monotonic()
    async with async_session() as session:
        for m in targets:
            row = await session.get(Memory, m.id)
            if row is None:
                continue
            row.status = "discarded"
            tags = set(_tags_of(row))
            tags.add(PURGE_TAG)
            row.tags = sorted(tags)
        await session.commit()

    print(f"\n[apply] 软删 {len(targets)} 条，耗时 {time.monotonic()-t0:.1f}s，标签 {PURGE_TAG}")
    print(f"快照（回滚用）：{snap}")
    print("回滚：把快照里的 id 逐条 UPDATE status='active' 并移除该标签")


if __name__ == "__main__":
    asyncio.run(main())

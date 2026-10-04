#!/usr/bin/env python3
"""聊天室 session 归并 · CLI 壳。

真正的逻辑在 ``gateway.core.dedupe.prune_chatroom_sessions``（dream light 也调用同一个
函数，保证「手动跑」和「每晚自愈」口径完全一致）。

背景：同一场 agent-chatroom session 会被 openclaw / codex / claude 等各自视角各存
一份——它们是**近似重复**（字面不同），md5 精确去重抓不到，只能按 tags 里的 session
UUID 归并。2026-09-29 实测 25 条 → 4 条。

规矩：默认 dry-run、只软删（标签 ``dedupe:chatroom-session``）、每场保留内容最长那份、
受保护整组跳过、写前落快照。建议先跑 scripts/backup_hcc.sh。

用法：
    env -u PYTHONPATH python scripts/dedupe_chatroom_sessions.py --dry-run
    env -u PYTHONPATH python scripts/dedupe_chatroom_sessions.py --apply
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

from gateway.core.database import async_session  # noqa: E402
from gateway.core.dedupe import _session_of, prune_chatroom_sessions  # noqa: E402
from gateway.models import Memory  # noqa: E402

SNAPSHOT_DIR = Path.home() / "backups" / "hcc"


async def main() -> None:
    ap = argparse.ArgumentParser(description="聊天室 session 归并（只软删，默认 dry-run）")
    ap.add_argument("--apply", action="store_true", help="真正写库；不加则只试算")
    ap.add_argument("--dry-run", action="store_true", help="只试算（本就是默认行为）")
    args = ap.parse_args()

    async with async_session() as session:
        stats = await prune_chatroom_sessions(session, dry_run=(not args.apply or args.dry_run))

        print(f"重复场次：{stats['groups']} 场，可省副本：{stats['discarded']} 份（跳过受保护 {stats['skipped_protected']} 场）")

        if not args.apply or args.dry_run:
            print("\n[dry-run] 未写库。加 --apply 才执行。")
            return

        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        snap = SNAPSHOT_DIR / f"dedupe_chatroom_sessions_{stamp}.json"
        details = []
        for mid in stats["discarded_ids"]:
            row = await session.get(Memory, mid)
            if row is not None:
                details.append({"id": row.id, "session": _session_of(row), "len": len(row.content or ""), "agent": row.agent_id})
        snap.write_text(json.dumps(details, ensure_ascii=False, indent=1), encoding="utf-8")

        t0 = time.monotonic()
        await session.commit()
        print(f"\n[apply] 软删 {stats['discarded']} 份，耗时 {time.monotonic()-t0:.1f}s，标签 dedupe:chatroom-session")
        print(f"快照（回滚用）：{snap}")


if __name__ == "__main__":
    asyncio.run(main())

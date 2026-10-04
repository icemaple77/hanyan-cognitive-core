#!/usr/bin/env python3
"""把不该进记忆库的系统记录软删掉(status=discarded,可恢复)。默认只统计,--apply 才动。

清的是这几类(2026-10-04 盘点):
- test        测试 agent 写的测试句
- session_end OpenClaw 插件的 [OpenClaw session_end] 系统记录
- intersess   agent 之间互发的 [Inter-session message] / [Subagent Context]
- bgproc      Hermes 的后台进程完成通知
- expert_en   采集器从专家 agent 会话收进来的纯英文工作输出(含烟自己不说纯英文)

带受保护标签(回忆录/永久承诺…)的行一律不动。动手前把命中的 id 和类别导出到
~/Backups/hcc/pollution-<时间>.jsonl;恢复 = 把这些 id 的 status 改回 active。
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402

from core.forget import PROTECTED_TAGS  # noqa: E402
from gateway.core.database import async_session  # noqa: E402

TAG = "pollution_cleanup_20261004"
CATEGORIES = {
    "test": "agent_id in ('hcc-audit-test','test') or content like '嵌入后端切到 soul 器官的验证记忆,可删%'",
    "session_end": "content like '[OpenClaw session_end]%'",
    "intersess": "content ~ '^user: \\[(Inter-session message|Subagent Context)' "
                 "or (source = 'dream' and content like '综合自 1 条%[Inter-session message]%')",
    "bgproc": "content ~ '^User: \\[IMPORTANT: Background process'",
    "expert_en": "source = 'harvester:openclaw' and content ~ '^assistant: [A-Za-z]' and content !~ '[一-龥]'",
}


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    hits: dict[str, str] = {}
    counts: dict[str, int] = {}
    async with async_session() as s:
        for cat, where in CATEGORIES.items():
            rows = (await s.execute(text(
                f"select id, tags from memories where status = 'active' and ({where})"))).all()
            kept = [r.id for r in rows if not (set(r.tags or []) & PROTECTED_TAGS)]
            counts[cat] = len(kept)
            for i in kept:
                hits.setdefault(str(i), cat)
        out = None
        if a.apply and hits:
            out = Path.home() / "Backups" / "hcc" / f"pollution-{dt.datetime.now():%Y%m%d-%H%M%S}.jsonl"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text("".join(json.dumps({"id": i, "category": c}) + "\n" for i, c in hits.items()))
            await s.execute(text(
                "update memories set status = 'discarded', "
                "tags = (coalesce(tags::jsonb, '[]'::jsonb) || to_jsonb(cast(:tag as text)))::json, "
                "updated_at = (now() at time zone 'utc') "
                "where id = any(:ids) and status = 'active'"), {"tag": TAG, "ids": list(hits)})
            await s.commit()
    print(json.dumps({"applied": bool(a.apply), "total": len(hits), **counts,
                      "export": str(out) if out else None}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

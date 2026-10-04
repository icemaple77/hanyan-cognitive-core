#!/usr/bin/env python3
"""按当前的入库规则(daily_digest.clean_item)把已入库的每日摘要再规整一遍。可重复运行。

- 主语是含烟的"偏好" → 软删(status=discarded,打 digest_cleanup 标签)
- 带"当前/今天/昨夜"的"事实" → 改成事件
- 正文尾巴上漏出来的来源编号 → 去掉
改动清单写到 ~/Backups/hcc/digest-cleanup-<时间>.jsonl。默认只统计,--apply 才动。

⚠️ 直接改库必须同时更新 updated_at:这些记忆会导出成知识库 Markdown(QMD),sync_from_qmd
每隔几分钟把文件读回库;库里的 updated_at 不比文件新,就会被文件里的旧 type/status 盖回去
(2026-10-05 第一次清理没更新它,2 分钟后 74 条改动全被还原)。
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import text  # noqa: E402

from gateway.core.database import async_session  # noqa: E402

_spec = importlib.util.spec_from_file_location("daily_digest", ROOT / "scripts" / "daily_digest.py")
dd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dd)

TYPE_TO_KIND = {v: k for k, v in dd.KIND_TO_TYPE.items()}
TAG = "digest_cleanup"


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    counts = {"checked": 0, "discarded": 0, "retyped": 0, "stripped": 0}
    log: list[dict] = []
    async with async_session() as s:
        rows = (await s.execute(text(
            "select id, type, summary, content from memories where status = 'active' and source = 'daily_digest'"))).all()
        for mid, mtype, summary, content in rows:
            counts["checked"] += 1
            kind, new = dd.clean_item(TYPE_TO_KIND.get(mtype, mtype), summary or "")
            if kind is None:
                counts["discarded"] += 1
                log.append({"id": str(mid), "action": "discarded"})
                if a.apply:
                    await s.execute(text(
                        "update memories set status = 'discarded', "
                        "tags = (coalesce(tags::jsonb, '[]'::jsonb) || to_jsonb(cast(:tag as text)))::json, "
                        "updated_at = (now() at time zone 'utc') "
                        "where id = :i"), {"i": mid, "tag": TAG})
                continue
            new_type = dd.KIND_TO_TYPE.get(kind, mtype)
            if new_type == mtype and new == (summary or ""):
                continue
            counts["retyped" if new_type != mtype else "stripped"] += 1
            log.append({"id": str(mid), "action": "retyped" if new_type != mtype else "stripped", "from": mtype})
            if a.apply:
                prefix = content[:13] if (content or "").startswith("[") else ""
                await s.execute(text("update memories set type = :t, summary = :sm, content = :c, "
                                     "updated_at = (now() at time zone 'utc') where id = :i"),
                                {"t": new_type, "sm": new, "c": f"{prefix}{new}", "i": mid})
        if a.apply:
            await s.commit()
    out = None
    if a.apply and log:
        out = Path.home() / "Backups" / "hcc" / f"digest-cleanup-{dt.datetime.now():%Y%m%d-%H%M%S}.jsonl"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("".join(json.dumps(x) + "\n" for x in log))
    print(json.dumps({"applied": a.apply, **counts, "log": str(out) if out else None}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

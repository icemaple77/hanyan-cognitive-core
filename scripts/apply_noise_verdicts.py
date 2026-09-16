"""把离线判定结果写回库(丢弃/打标),用于在 Umbrella 上批量判完后落库。

为什么要离线判:本机 Ollama 串行约 0.5s/条,1.5 万条要两个多小时;同一个
GGUF 放到 Umbrella 的 llama-server 上开 8 路并发是 22 条/秒,十几分钟跑完。
判定在 Umbrella,落库在 Mac —— 本脚本只做落库这一半。

写回规则与 ``core/noise_filter_events`` 完全一致:
  keep=false → status=discarded(软删除,可恢复)+ 打复核标签
  keep=true  → **只打标签**,importance 一个字不动(降噪不负责重新定价)
  低信任日志(tool_result / openclaw_plugin)的 keep → importance 封顶 0.4

输入 JSONL 每行 {"id": ..., "keep": 0|1}(judge_remote.py 的输出格式)。
默认干跑,``--apply`` 才写库;写前强制要求一份当日 pg_dump 存在。

    uv run python scripts/apply_noise_verdicts.py verdicts.jsonl
    uv run python scripts/apply_noise_verdicts.py verdicts.jsonl --apply
"""

from __future__ import annotations

import argparse
import asyncio
import glob
import json
import os
import sys
from collections import Counter

import asyncpg

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.config import core_settings  # noqa: E402
from core.local_filter import DISCARDED_STATUS, NOISE_FILTER_TAG  # noqa: E402
from core.noise_filter_events import LOW_TRUST_IMPORTANCE_CAP, LOW_TRUST_SOURCES, LOW_TRUST_TYPES  # noqa: E402

BACKUP_GLOB = os.path.expanduser("~/Backups/hcc-db/hcc-*.dump")


def _recent_backup() -> str | None:
    dumps = sorted(glob.glob(BACKUP_GLOB), key=os.path.getmtime)
    if not dumps:
        return None
    return dumps[-1]


async def main(path: str, apply: bool) -> None:
    verdicts = {}
    for line in open(path):
        row = json.loads(line)
        if "keep" in row:
            verdicts[row["id"]] = int(row["keep"])
    print(f"判定 {len(verdicts)} 条(保留 {sum(verdicts.values())} / 丢弃 {len(verdicts)-sum(verdicts.values())})")

    backup = _recent_backup()
    if apply and not backup:
        sys.exit(f"拒绝写库:{BACKUP_GLOB} 下没有任何备份。先跑 pg_dump -Fc 再来。")
    print(f"备份:{backup}")

    conn = await asyncpg.connect(core_settings.database_url.replace("+asyncpg", ""))
    try:
        rows = await conn.fetch(
            "select id, coalesce(type,'') t, coalesce(source,'') src, importance, status from memories where id = any($1)",
            list(verdicts),
        )
        stats: Counter[str] = Counter()
        for r in rows:
            if r["status"] != "active":
                stats["跳过(已非活跃)"] += 1
                continue
            low_trust = r["t"] in LOW_TRUST_TYPES or r["src"] in LOW_TRUST_SOURCES
            if verdicts[r["id"]] == 0:
                stats["丢弃"] += 1
                if apply:
                    await conn.execute(
                        "update memories set status=$1, tags = (select to_json(array(select distinct e from unnest("
                        "  array(select json_array_elements_text(coalesce(tags,'[]'::json))) || $2::text[]) e)))"
                        " where id=$3",
                        DISCARDED_STATUS, [NOISE_FILTER_TAG], r["id"],
                    )
            else:
                capped = min(float(r["importance"] or 0.0), LOW_TRUST_IMPORTANCE_CAP) if low_trust else None
                stats["保留(日志封顶)" if capped is not None else "保留(重要度不动)"] += 1
                if apply:
                    if capped is not None:
                        await conn.execute("update memories set importance=$1 where id=$2", capped, r["id"])
                    await conn.execute(
                        "update memories set tags = (select to_json(array(select distinct e from unnest("
                        "  array(select json_array_elements_text(coalesce(tags,'[]'::json))) || $1::text[]) e)))"
                        " where id=$2",
                        [NOISE_FILTER_TAG], r["id"],
                    )
        missing = len(verdicts) - len(rows)
        if missing:
            stats["库里已不存在"] = missing
        for k, v in stats.most_common():
            print(f"  {k:<18} {v}")
        print("（干跑,未写库。加 --apply 才真写）" if not apply else "已写库。恢复某条:update memories set status='active' where id=...")
    finally:
        await conn.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("verdicts")
    p.add_argument("--apply", action="store_true")
    args = p.parse_args()
    asyncio.run(main(args.verdicts, args.apply))

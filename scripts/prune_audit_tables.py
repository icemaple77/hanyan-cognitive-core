"""审计表保留期清理:dream_signals / memory_conflicts / dream_runs。

为什么需要(2026-09-16 查实):
- ``dream_signals`` 11.3 万行 / 39MB,从 2026-08-04 起从没清过。它只被
  ``core/dream.py`` 读"当天/最近一次"的信号,历史行只占空间。
- ``memory_conflicts`` 1.45 万行,全仓库**只有写入**(``gateway/services``
  的 ``_flag_stale_duplicates``),没有任何读取方——是纯审计流水。
- ``dream_runs`` 每天 3~5 行,stats 里塞了 promoted_memories 全量 JSON,
  254 行已经 6.4MB。

只删过期行,不碰 ``memories``。默认干跑,``--apply`` 才真写。

    uv run python scripts/prune_audit_tables.py            # 看看会删多少
    uv run python scripts/prune_audit_tables.py --apply
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

import asyncpg

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.config import core_settings  # noqa: E402

# 保留期:dreaming 只读当天信号,30 天足够回溯"最近为什么晋升了这条";
# 冲突流水留 90 天,给"这条记忆什么时候被判定为陈旧重复"留查证窗口。
RETENTION_DAYS = {
    "dream_signals": int(os.getenv("HCC_PRUNE_DREAM_SIGNALS_DAYS", "30")),
    "memory_conflicts": int(os.getenv("HCC_PRUNE_MEMORY_CONFLICTS_DAYS", "90")),
    "dream_runs": int(os.getenv("HCC_PRUNE_DREAM_RUNS_DAYS", "180")),
}


async def main(apply: bool) -> None:
    conn = await asyncpg.connect(core_settings.database_url.replace("+asyncpg", ""))
    try:
        for table, days in RETENTION_DAYS.items():
            where = f"created_at < now() - interval '{days} days'"
            if table == "dream_runs":
                where = f"started_at < now() - interval '{days} days'"
            total = await conn.fetchval(f"select count(*) from {table}")
            stale = await conn.fetchval(f"select count(*) from {table} where {where}")
            size = await conn.fetchval(f"select pg_size_pretty(pg_total_relation_size('{table}'))")
            print(f"{table:<18} 共 {total:>7} 行 / {size:<8} 超过 {days} 天的 {stale:>7} 行", end="")
            if apply and stale:
                await conn.execute(f"delete from {table} where {where}")
                await conn.execute(f"vacuum (analyze) {table}")
                after = await conn.fetchval(f"select pg_size_pretty(pg_total_relation_size('{table}'))")
                print(f" → 已删,现 {after}")
            else:
                print(" (干跑)" if stale else "")
    finally:
        await conn.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--apply", action="store_true", help="真正删除(默认只统计)")
    asyncio.run(main(p.parse_args().apply))

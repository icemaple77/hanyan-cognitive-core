"""把软删除的记忆搬进归档表,给主表和它的索引减负。

为什么(2026-09-16 查实):``memories`` 27479 行 / 307MB,其中 9235 行是
``status='discarded'`` 的噪音 —— 它们永远不会被检索(所有检索路径都
白名单 ``status='active'``),却和活跃数据共用同一批索引,包括新建的
768 维 HNSW(99MB)。

搬,不是删:整行原样插入 ``memories_archive``(同结构 + ``archived_at``),
主表删行。要恢复某条,从归档表插回去即可,脚本末尾打印了原句。

默认干跑,``--apply`` 才动;写前要求 ``~/Backups/hcc-db`` 下存在 pg_dump。

    uv run python scripts/archive_discarded.py            # 看会搬多少
    uv run python scripts/archive_discarded.py --apply --older-than-days 7
"""

from __future__ import annotations

import argparse
import asyncio
import glob
import os
import sys

import asyncpg

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.config import core_settings  # noqa: E402

BACKUP_GLOB = os.path.expanduser("~/Backups/hcc-db/hcc-*.dump")


async def main(days: int, apply: bool) -> None:
    conn = await asyncpg.connect(core_settings.database_url.replace("+asyncpg", ""))
    try:
        await conn.execute(
            "create table if not exists memories_archive "
            "(like memories including defaults including indexes including constraints)"
        )
        await conn.execute("alter table memories_archive add column if not exists archived_at timestamp default now()")

        where = f"status = 'discarded' and updated_at < now() - interval '{days} days'"
        n = await conn.fetchval(f"select count(*) from memories where {where}")
        before = await conn.fetchval("select pg_size_pretty(pg_total_relation_size('memories'))")
        print(f"待归档 {n} 行(discarded 且 {days} 天内没再动过);主表现 {before}")

        if not apply:
            print("(干跑,未改动。加 --apply 才搬)")
            return
        if not sorted(glob.glob(BACKUP_GLOB), key=os.path.getmtime):
            sys.exit(f"拒绝改库:{BACKUP_GLOB} 下没有备份。先 pg_dump -Fc。")

        async with conn.transaction():
            cols = [r["column_name"] for r in await conn.fetch(
                "select column_name from information_schema.columns where table_name='memories' order by ordinal_position")]
            col_list = ", ".join(f'"{c}"' for c in cols)
            moved = await conn.fetchval(
                f"with moved as (delete from memories where {where} returning *) "
                f"insert into memories_archive ({col_list}) select {col_list} from moved returning 1", column=0)
            print(f"已搬 {n} 行(返回 {moved})")
        await conn.execute("vacuum (analyze) memories")
        after = await conn.fetchval("select pg_size_pretty(pg_total_relation_size('memories'))")
        print(f"主表 {before} → {after}")
        print("恢复单条: insert into memories select <列> from memories_archive where id='...'; delete from memories_archive where id='...';")
    finally:
        await conn.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--older-than-days", type=int, default=7)
    p.add_argument("--apply", action="store_true")
    a = p.parse_args()
    asyncio.run(main(a.older_than_days, a.apply))

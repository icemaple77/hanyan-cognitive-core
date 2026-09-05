#!/usr/bin/env python3
"""清掉 dreaming 重复产出的巩固记忆(2026-09-05)。

病因见 core/dream.py 里 _group_for_knowledge 的注释:簇的 key 曾用成员 id 的
精确集合去哈希,而成员集每晚必变,于是同一个话题每晚新建一条、旧的还留着。
查到时 26 组**全文逐字相同**、共 103 条。

去重规则只认**逐字相同**(md5),不做模糊判断:
  同一组里保留**最早**的那条(它承载了最长的 access_count 历史和被引用关系),
  其余标记 status='discarded' —— 不硬删,和噪音过滤保持一致的姿势。

默认 dry-run,--apply 才写;写前落全文快照。
"""
from __future__ import annotations
import argparse, asyncio, json, sys
from datetime import datetime

from sqlalchemy import text as sql_text
from gateway.core.database import async_session

DISCARD_TAG = "dedupe:dream-knowledge"


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真写库(默认只试算)")
    a = ap.parse_args()

    async with async_session() as s:
        rows = (await s.execute(sql_text(
            "select md5(content) h, id, created_at, access_count, "
            "       left(replace(coalesce(nullif(summary,''),content), chr(10), ' '), 60) preview, content "
            "from memories "
            "where type='knowledge' and source='dream' and status='active' "
            "  and md5(content) in ("
            "     select md5(content) from memories "
            "     where type='knowledge' and source='dream' and status='active' "
            "     group by 1 having count(*) > 1) "
            "order by h, created_at"))).all()

    groups: dict[str, list] = {}
    for r in rows:
        groups.setdefault(r[0], []).append(r)

    if not groups:
        print("没有重复组")
        return 0

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    snap = f"scripts/_dream_dedupe_snapshot_{stamp}.json"
    to_discard = []
    for h, g in groups.items():
        keep, drop = g[0], g[1:]          # 已按 created_at 排序,留最早的
        to_discard += [d[1] for d in drop]

    with open(snap, "w", encoding="utf-8") as fh:
        json.dump({r[1]: {"content": r[5], "created_at": str(r[2])} for r in rows},
                  fh, ensure_ascii=False)
    print(f"快照: {snap}\n")
    print(f"{len(groups)} 个重复组 · 共 {len(rows)} 条 · 保留 {len(groups)} 条 · 丢弃 {len(to_discard)} 条\n")
    for h, g in sorted(groups.items(), key=lambda kv: -len(kv[1]))[:12]:
        print(f"  ×{len(g):<3} 留 {g[0][2].strftime('%m-%d')} · 丢 {len(g)-1} 条   {g[0][4]}")

    if not a.apply:
        print("\n[试算] 没有写库。确认后加 --apply")
        return 0

    async with async_session() as s:
        await s.execute(sql_text(
            # 用 cast(:tag as text) 而不是 :tag::text —— 后者里的 `::`
            # 会和 SQLAlchemy 的 `:name` 参数绑定语法撞上,报
            # "syntax error at or near :"(踩过)
            "update memories set status='discarded', "
            "  tags = cast((cast(tags as jsonb) || to_jsonb(cast(:tag as text))) as json) "
            "where id = any(:ids)"), {"tag": DISCARD_TAG, "ids": to_discard})
        await s.commit()
    print(f"\n已丢弃 {len(to_discard)} 条(status='discarded',带 {DISCARD_TAG} 标签,未硬删)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

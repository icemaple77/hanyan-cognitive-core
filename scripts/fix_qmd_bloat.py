#!/usr/bin/env python3
"""修复被 QMD 往返 bug 撑大的记忆(2026-09-04)。

病因见 core/sync_engine.py 里 parse_qmd_file 的那段注释:摘要引用块没被剥掉,
每轮同步往 content 里多追加一份自己,于是内容变成同一个块重复 N 次。

修法**不是**从备份恢复:备份里也已经带着膨胀(9-03 的 31MB、9-04 的 72MB),
而且恢复会把这几天其他所有改动一起回滚。这里用的是无损去重——

    content == 单元 × N  (逐字节完全相等)

只有等式**精确成立**才写回,否则一律不动、只报告。这样"修复"这件事本身
不依赖任何判断:要么可证明无损,要么不碰。

默认 dry-run。--apply 才真写,写前把全部原文落盘。
"""
from __future__ import annotations
import argparse, json, os, subprocess, sys
from datetime import datetime

PSQL = ["psql", "-U", "hcc", "-d", "hcc", "-tAc"]

def q(sql: str) -> str:
    r = subprocess.run(PSQL + [sql], capture_output=True, text=True)
    if r.returncode: raise RuntimeError(r.stderr.strip())
    return r.stdout

def plan_dedup(content: str, summary: str) -> tuple[str, dict] | None:
    """算出去重后的 content。只有**可证明无损**时才返回,否则 None。

    实际结构(查证得到,不是猜):按空行切开后只有 7~8 种不同的块,其中一块是
    生成器写的摘要引用块 `> <summary>`,重复了上万次;其余每块**各出现一次**,
    那才是真正的正文,一个字都没丢,只是被埋了。

    所以去重规则是精确的:把那个重复的摘要块**全部**删掉(它本来就不该在
    content 里 —— 摘要有自己的列,生成器每次还会重新写一遍),其余块原样保留、
    保持原顺序。

    三条断言,任一不成立就返回 None 交给人看:
      1. 重复块有且只有一个,且以 ">" 开头
      2. 它去掉 "> " 之后确实等于该记忆 summary 列的值 —— 这一条最关键:
         它证明删掉的是"别处已有的副本",而不是正文
      3. 只出现一次的块,去重前后逐块相等且顺序不变
    """
    blocks = content.split("\n\n")
    from collections import Counter
    counts = Counter(blocks)
    repeated = [b for b, n in counts.items() if n > 1]

    if len(repeated) != 1:
        return None                                   # 断言 1
    dup = repeated[0]
    if not dup.lstrip().startswith(">"):
        return None                                   # 断言 1
    if dup.lstrip().lstrip(">").strip() != summary.strip():
        return None                                   # 断言 2:删的必须是 summary 的副本

    kept = [b for b in blocks if b != dup]
    singles_before = [b for b in blocks if counts[b] == 1]
    if kept != singles_before:
        return None                                   # 断言 3:顺序/内容不得变

    new = "\n\n".join(kept).strip()
    return new, {
        "removed_copies": counts[dup],
        "removed_block_len": len(dup),
        "kept_blocks": len(kept),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真写库(默认只试算)")
    ap.add_argument("--min-len", type=int, default=10000)
    a = ap.parse_args()

    ids = [x for x in q(f"select id from memories where length(content) > {a.min_len} order by length(content) desc").split("\n") if x.strip()]
    print(f"候选 {len(ids)} 条(content > {a.min_len} 字符)\n")

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    snap_path = f"scripts/_qmd_bloat_snapshot_{stamp}.json"
    snapshot, plan, untouched = {}, [], []
    saved = 0

    for mid in ids:
        content = q(f"select content from memories where id='{mid}'").rstrip("\n")
        summary = q(f"select coalesce(summary,'') from memories where id='{mid}'").strip()
        snapshot[mid] = content
        res = plan_dedup(content, summary)
        if res is None:
            untouched.append((mid, len(content), summary[:40]))
            continue
        new, info = res
        plan.append((mid, len(content), len(new), info["removed_copies"], summary[:40], new))
        saved += len(content) - len(new)

    with open(snap_path, "w", encoding="utf-8") as fh:
        json.dump(snapshot, fh, ensure_ascii=False)
    print(f"原文快照已落盘: {snap_path}  ({os.path.getsize(snap_path)/1048576:.1f} MB)\n")

    print(f"── 可无损去重 {len(plan)} 条 ──")
    for mid, old, ulen, n, sm, _new in plan[:80]:
        print(f"  {old:>9,} → {ulen:>6,} 字符  (删掉 {n:>6,} 份摘要副本)  {sm}")
    print(f"\n  合计回收 {saved/1048576:.1f} MB 正文")

    if untouched:
        print(f"\n── 不是整数份重复、**不碰** {len(untouched)} 条 ──")
        for mid, ln, s in untouched[:20]:
            print(f"  {ln:>9,} 字符  {mid}  {s}")
        print("  (这些需要人看过再定,脚本不猜)")

    if not a.apply:
        print("\n[试算] 没有写库。确认无误后加 --apply")
        return 0

    # 用仓库自己的 SQLAlchemy 层写,不走 psql:
    # psql 的 -c 不做 :'var' 变量插值(试过,76 条全报 syntax error、一行没改),
    # 而把几千字符的正文拼进 SQL 字面量则要自己处理引号转义 —— 那是自找的坑。
    # 参数化查询是唯一正确的写法。
    print("\n[写入中]")
    import asyncio
    from sqlalchemy import text as sql_text
    from gateway.core.database import async_session

    async def write_all():
        ok = fail = 0
        async with async_session() as session:
            for mid, old, ulen, n, sm, new in plan:
                try:
                    await session.execute(
                        # id 列是 character varying,不是 uuid —— 别加 cast(:i as uuid),
                        # 那会得到 "operator does not exist: character varying = uuid"
                        sql_text("update memories set content = :c, updated_at = now() "
                                 "where id = :i"),
                        {"c": new, "i": mid},
                    )
                    ok += 1
                    print(f"  ✓ {old:>9,} → {ulen:>6,}  {sm}")
                except Exception as exc:
                    fail += 1
                    print(f"  ✗ {mid}: {str(exc)[:110]}")
            await session.commit()
        print(f"\n  成功 {ok} · 失败 {fail}")
        return fail

    if asyncio.run(write_all()):
        print("  有失败项,请复核后再重跑(快照仍在)")
        return 1
    print("\n⚠️ content 变了,这些记忆的向量已过期,需重嵌入:")
    print("   .venv/bin/python scripts/reembed_all.py --ids-from " + snap_path)
    return 0

if __name__ == "__main__":
    sys.exit(main())

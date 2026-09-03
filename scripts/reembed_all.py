#!/usr/bin/env python3
"""全库重嵌入 —— 换向量模型时的**唯一正规通道**。

为什么有这个脚本(2026-09-03 事故复盘):

    2026-08-29 换模型(qwen3-embedding:0.6b/1024 → bge-base-zh-v1.5/768,后者
    常驻仅 ~400MB 内存,前者 1.6G,故换)时,迁移是**手工在线上敲 SQL** 完成的。
    后果有三:
      1. 漏表 —— 只重算了 memories 的 4673 条,documents 的 245 行没动,列停在
         vector(1024)。之后每次文档语义检索都报 "different vector dimensions",
         知识召回静默降级成纯 BM25,**5 天无人察觉**;
      2. 约定漂移 —— 手工重算按 content 单独嵌,而线上写入路径按 content+summary
         嵌,库里从此混着两套文本约定(重算余弦 0.92~0.99);
      3. 无痕迹 —— 迁移动作不在仓库里,无法复查、无法重跑。

    所以:换模型不再手工敲 SQL,跑这个脚本。它覆盖**所有**向量表、用**唯一**的
    规范文本函数(gateway.core.embeddings.*_embedding_text)、可 dry-run、可重跑。

用法::

    # 看看会改什么,不动数据
    python scripts/reembed_all.py --dry-run

    # 按当前配置(core_settings.embedding_dim)重算全部表
    python scripts/reembed_all.py

    # 只重算某张表
    python scripts/reembed_all.py --table memories

    # 换模型后列维度变了:先改列型(会清空旧向量)再重算
    python scripts/reembed_all.py --migrate-dim

注意:``--migrate-dim`` 会 ``UPDATE ... SET embedding = NULL`` 再 ALTER 列型——
旧向量不可恢复(换模型后它们本就不可用)。正文/摘要等内容**从不改动**。
"""
from __future__ import annotations

import argparse
import asyncio
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402

from core.config import core_settings  # noqa: E402
from gateway.core.database import engine  # noqa: E402
from gateway.core.embeddings import (  # noqa: E402
    EMBEDDING_MODEL,
    EMBEDDING_PROVIDER,
    document_embedding_text,
    embed_text,
    memory_embedding_text,
)

_VECTOR_TYPE_RE = re.compile(r"vector\((\d+)\)")

# 每张表:主键、取哪些字段、以及如何拼出规范嵌入文本。
# 新增带向量的表时,在这里加一行即可 —— 迁移逻辑不用改。
TABLES: dict[str, dict] = {
    "memories": {
        # 覆盖**所有状态**,不只 active:归档/丢弃的记忆构成遗忘 isolation 区,
        # 按设计是要能被单独检索的——它们的向量若停在旧模型/旧约定,查出来就是
        # 垃圾。一致性必须含归档区。(2026-09-03:首版漏了这点,3122 行差点被漏。)
        "select": "SELECT id, content, coalesce(summary,'') FROM memories WHERE content IS NOT NULL",
        "count": "SELECT count(*) FROM memories WHERE content IS NOT NULL",
        "text": lambda row: memory_embedding_text(row[1], row[2]),
    },
    "documents": {
        "select": "SELECT id, title, content FROM documents WHERE content IS NOT NULL AND content <> ''",
        "count": "SELECT count(*) FROM documents WHERE content IS NOT NULL AND content <> ''",
        "text": lambda row: document_embedding_text(row[1], row[2]),
    },
}

# --stale-only 的过滤条件:只挑"不是当前模型算的"行(含从未标记的历史行)。
_STALE_CLAUSE = "(embedding_model IS DISTINCT FROM :model)"


def _with_stale(sql: str, stale_only: bool) -> str:
    """给 select/count 语句追加 stale 过滤(它们都已带 WHERE)。"""
    return f"{sql} AND {_STALE_CLAUSE}" if stale_only else sql


async def _column_dim(conn, table: str) -> int | None:
    row = (await conn.execute(text("""
        SELECT format_type(a.atttypid, a.atttypmod)
        FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid
        WHERE c.relname = :t AND a.attname = 'embedding' AND NOT a.attisdropped
    """), {"t": table})).scalar()
    if not row:
        return None
    m = _VECTOR_TYPE_RE.search(row)
    return int(m.group(1)) if m else None


def _vec_literal(vec: list[float]) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in vec) + "]"


async def reembed_table(table: str, *, dry_run: bool, migrate_dim: bool, batch: int,
                        stale_only: bool = False) -> None:
    spec = TABLES[table]
    target_dim = core_settings.embedding_dim
    params = {"model": EMBEDDING_MODEL}

    async with engine.begin() as conn:
        dim = await _column_dim(conn, table)
        total = (await conn.execute(
            text(_with_stale(spec["count"], stale_only)), params)).scalar() or 0
        # 来源分布:一眼看出库里混着几个模型的向量(08-29 漏迁移那种事故的探照灯)
        prov = list(await conn.execute(text(
            f"SELECT coalesce(embedding_model,'(未标记)'), count(*) FROM {table} "
            f"WHERE embedding IS NOT NULL GROUP BY 1 ORDER BY 2 DESC")))

    print(f"\n[{table}] 列维度={dim} 目标维度={target_dim} 待重算={total} 行"
          + ("(仅 stale)" if stale_only else ""))
    if prov:
        print("  现有向量来源:", ", ".join(f"{m}×{n}" for m, n in prov))
    if dim is None:
        print(f"  跳过:{table} 没有 embedding 列")
        return

    if dim != target_dim:
        if not migrate_dim:
            print(f"  ❌ 列维度 {dim} ≠ 目标 {target_dim}。加 --migrate-dim 才会改列型"
                  f"(会清空旧向量——换模型后它们本就不可用)。")
            return
        if dry_run:
            print(f"  [dry-run] 将清空旧向量并把列改成 vector({target_dim})")
        else:
            async with engine.begin() as conn:
                await conn.execute(text(f"UPDATE {table} SET embedding = NULL"))
                await conn.execute(text(
                    f"ALTER TABLE {table} ALTER COLUMN embedding TYPE vector({target_dim})"))
            print(f"  ✅ 列已改为 vector({target_dim})(旧向量已清,正文未动)")

    if dry_run:
        print(f"  [dry-run] 将用 {EMBEDDING_PROVIDER}/{EMBEDDING_MODEL} 重算 {total} 行")
        return

    async with engine.begin() as conn:
        rows = list(await conn.execute(text(_with_stale(spec["select"], stale_only)), params))

    done = 0
    pending: list[tuple[str, str]] = []
    for row in rows:
        vec = embed_text(spec["text"](row))
        pending.append((row[0], _vec_literal(vec)))
        if len(pending) >= batch:
            await _flush(table, pending)
            done += len(pending); pending.clear()
            print(f"    …{done}/{len(rows)}", flush=True)
    if pending:
        await _flush(table, pending)
        done += len(pending)
    print(f"  ✅ {table} 重算完成 {done} 行")


async def _flush(table: str, pending: list[tuple[str, str]]) -> None:
    async with engine.begin() as conn:
        for rid, lit in pending:
            await conn.execute(
                text(f"UPDATE {table} SET embedding = '{lit}'::vector, "
                     f"embedding_model = :m WHERE id = :i"),
                {"i": rid, "m": EMBEDDING_MODEL})


async def main() -> None:
    ap = argparse.ArgumentParser(description="全库重嵌入(换向量模型的正规通道)")
    ap.add_argument("--table", choices=[*TABLES, "all"], default="all")
    ap.add_argument("--dry-run", action="store_true", help="只报告,不改数据")
    ap.add_argument("--migrate-dim", action="store_true",
                    help="列维度与配置不符时改列型(会清空旧向量)")
    ap.add_argument("--batch", type=int, default=200, help="每批写回行数")
    ap.add_argument("--stale-only", action="store_true",
                    help="只重算不是当前模型算的行(换模型后增量补跑/断点续跑)")
    args = ap.parse_args()

    print(f"嵌入后端:{EMBEDDING_PROVIDER} / {EMBEDDING_MODEL} / {core_settings.embedding_dim} 维")
    targets = list(TABLES) if args.table == "all" else [args.table]
    for t in targets:
        await reembed_table(t, dry_run=args.dry_run, migrate_dim=args.migrate_dim,
                            batch=args.batch, stale_only=args.stale_only)
    await engine.dispose()
    print("\n完成。建议随后重启网关,并确认 /health 的 vector_dims.ok == true。")


if __name__ == "__main__":
    asyncio.run(main())

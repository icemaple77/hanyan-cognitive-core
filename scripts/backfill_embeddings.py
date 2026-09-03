#!/usr/bin/env python3
"""Backfill pgvector embeddings for existing ``memories``/``documents`` rows.

体检报告 P0-1: embedding 默认跑的是 hash 后端(词法哈希,不是真语义),而且
截至这次迁移前的实际检查——``memories``/``documents`` 表里 embedding 列
100% 是 NULL(从没有任何调用方真正传过向量,见 mcp/memory_tools.py 的历史
说明)。所以这不是"从 hash 向量空间迁移到 ollama 向量空间"的重嵌入,而是
给两张表第一次真正算向量的回填(backfill)——本脚本名字仍叫 backfill 而不是
migrate,如实反映这一点。

用法(项目根目录,venv 激活,``.env`` 里 ``HCC_EMBEDDING_PROVIDER=ollama``)::

    env -u PYTHONPATH python scripts/backfill_embeddings.py --dry-run   # 先看有多少行要处理
    env -u PYTHONPATH python scripts/backfill_embeddings.py             # 实际回填 memories + documents
    env -u PYTHONPATH python scripts/backfill_embeddings.py --table memories  # 只回填一张表

安全性:
- 幂等/可断点续传——只处理 ``embedding IS NULL`` 的行,已经算过的行不会重算,
  中途 Ctrl-C 或崩溃后重新运行会从剩余的行继续,不会重复计费/重复请求 ollama。
- 分批提交(默认 50 行一批),不是单个大事务——避免一次性长事务锁表/OOM,
  单批失败只回滚这一批,已提交的批次不受影响。
- 只算向量、只 UPDATE embedding 列,不改 content/tags/status 等其它字段。
- 运行前建议先跑一次 ``scripts/backup_hcc.sh``(或等效 pg_dump)——本脚本不会
  自动备份,是纯粹的只写 embedding 列的操作,但"建议先备份"是体检报告明确
  要求的,交给运维习惯而不是脚本硬编码一次性备份逻辑。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select

from gateway.core.database import async_session
from gateway.core.embeddings import EMBEDDING_PROVIDER, embed_text
from gateway.models import Document, Memory

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("hcc.backfill_embeddings")

BATCH_SIZE = 50
CONCURRENCY = 4  # same default as HCC_NOISE_FILTER_CONCURRENCY (core/config.py) — one Ollama call each in flight

TABLES = {
    "memories": (Memory, lambda row: f"{row.content}\n{row.summary or ''}".strip()),
    "documents": (Document, lambda row: f"{row.title or ''}\n{row.content}".strip()),
}


async def _embed_row(row, text_of, semaphore: asyncio.Semaphore) -> tuple[object, Exception | None]:
    text = text_of(row)
    if not text.strip():
        return row, None  # nothing to embed — leave NULL rather than embed an empty string
    async with semaphore:
        try:
            row.embedding = await asyncio.to_thread(embed_text, text)
            return row, None
        except Exception as exc:
            return row, exc


async def backfill_table(name: str, dry_run: bool, batch_size: int, concurrency: int) -> None:
    model, text_of = TABLES[name]

    async with async_session() as session:
        total = (
            await session.execute(select(model.id).where(model.embedding.is_(None)))
        ).scalars().all()
    pending = len(total)
    logger.info("[%s] %d row(s) missing embedding (provider=%s)", name, pending, EMBEDDING_PROVIDER)

    if dry_run or pending == 0:
        return

    semaphore = asyncio.Semaphore(concurrency)
    done = 0
    errors = 0
    started = time.monotonic()

    while True:
        async with async_session() as session:
            stmt = select(model).where(model.embedding.is_(None)).limit(batch_size)
            batch = (await session.execute(stmt)).scalars().all()
            if not batch:
                break

            results = await asyncio.gather(*(_embed_row(row, text_of, semaphore) for row in batch))
            for row, exc in results:
                if exc is not None:
                    errors += 1
                    logger.error("[%s] failed to embed row %s: %s", name, row.id, exc)
                else:
                    done += 1

            await session.commit()

        elapsed = time.monotonic() - started
        rate = done / elapsed if elapsed > 0 else 0.0
        logger.info(
            "[%s] progress %d/%d embedded, %d error(s), %.1f rows/s",
            name, done, pending, errors, rate,
        )

    logger.info("[%s] done: %d embedded, %d error(s) in %.1fs", name, done, errors, time.monotonic() - started)


async def main_async(tables: list[str], dry_run: bool, batch_size: int, concurrency: int) -> None:
    if EMBEDDING_PROVIDER == "hash":
        logger.warning(
            "HCC_EMBEDDING_PROVIDER=hash — this writes lexical-hash placeholder vectors, "
            "not real semantic embeddings. Set HCC_EMBEDDING_PROVIDER=ollama in .env first "
            "unless you specifically want the hash fallback."
        )
    for name in tables:
        await backfill_table(name, dry_run=dry_run, batch_size=batch_size, concurrency=concurrency)


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill embeddings for memories/documents")
    parser.add_argument("--table", choices=list(TABLES), action="append", dest="tables",
                        help="Limit to one table (repeatable). Defaults to both.")
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    parser.add_argument("--concurrency", type=int, default=CONCURRENCY,
                        help="Concurrent Ollama embedding calls in flight (default 4, matches HCC_NOISE_FILTER_CONCURRENCY)")
    parser.add_argument("--dry-run", action="store_true", help="Only report how many rows are pending")
    args = parser.parse_args()

    tables = args.tables or list(TABLES)
    asyncio.run(main_async(tables, dry_run=args.dry_run, batch_size=args.batch_size, concurrency=args.concurrency))


if __name__ == "__main__":
    main()

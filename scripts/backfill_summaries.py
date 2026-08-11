#!/usr/bin/env python3
"""Backfill missing ``summary`` fields on HCC memories with a local LLM.

Motivation (2026-08-09 排查, P0-2)
---------------------------------
~98% of active memories (chiefly the ``openclaw_sync`` / ``hermes`` bulk-sync
sources) were stored as raw dialogue with an empty ``summary``. That hurts two
things at once:

* **Injection density** — ``/context`` falls back to the first raw content line,
  so every recalled memory costs many tokens for little signal.
* **Knowledge distillation** — the QMD generator titles/indexes documents from
  ``summary``; without it, knowledge docs are near-useless even once produced.

This script fills the gap with a *local* model (Ollama, zero API cost): for each
active memory lacking a summary it generates a one-line Chinese headline and
writes it back through the ORM, so the ``before_update`` hook refreshes
``search_text`` and (optionally) the embedding stays consistent with the new
content+summary text.

It is **idempotent and resumable** — it only ever selects rows whose summary is
still empty, so re-running (or scheduling it daily) simply picks up whatever has
accumulated since. Safe to Ctrl-C; committed batches persist.

Usage
-----
    python -m scripts.backfill_summaries                 # process everything
    python -m scripts.backfill_summaries --limit 50      # first 50 (smoke test)
    python -m scripts.backfill_summaries --dry-run       # print, don't write
    python -m scripts.backfill_summaries --reembed       # also refresh embeddings
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

import httpx
from sqlalchemy import select

# Allow running both as `python -m scripts.backfill_summaries` and directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gateway.core.database import async_session  # noqa: E402
from gateway.models import Memory, MemoryStatus  # noqa: E402

logger = logging.getLogger("backfill_summaries")

OLLAMA_URL = os.getenv("HCC_OLLAMA_URL", "http://127.0.0.1:11434")
SUMMARY_MODEL = os.getenv("HCC_SUMMARY_MODEL", "qwen3.5:2b")
# Content at/under this length is already its own best summary — copy it through
# without spending an LLM call.
SHORT_CONTENT_CHARS = 50
# Cap how much content we feed the model — the lead of a memory carries the
# topic; sending 10KB of transcript just slows generation without helping.
MAX_CONTENT_CHARS = 2000
MAX_SUMMARY_CHARS = 120
CONCURRENCY = 4

_SYSTEM_PROMPT = (
    "你是记忆摘要器。把用户给的记忆内容压缩成一句不超过40字的中文陈述句，"
    "点明主题与关键信息，供后续检索与知识索引使用。"
    "只输出摘要本身，不要引号、不要前缀、不要解释、不要思考过程。"
)


def _norm(text: str) -> str:
    return " ".join((text or "").split()).strip()


async def _summarize(client: httpx.AsyncClient, content: str) -> str | None:
    """Return a one-line summary for ``content``, or ``None`` on failure."""
    snippet = content.strip()[:MAX_CONTENT_CHARS]
    try:
        resp = await client.post(
            f"{OLLAMA_URL}/api/chat",
            json={
                "model": SUMMARY_MODEL,
                "think": False,  # qwen3.5 hybrid-reasoning: keep output, drop CoT
                "stream": False,
                "options": {"temperature": 0.2, "num_predict": 160},
                "messages": [
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": snippet},
                ],
            },
            timeout=120.0,
        )
        resp.raise_for_status()
        summary = _norm(resp.json().get("message", {}).get("content", ""))
        # Strip a stray leading label the small model sometimes emits.
        for prefix in ("摘要：", "摘要:", "总结：", "总结:"):
            if summary.startswith(prefix):
                summary = summary[len(prefix):].strip()
        return summary[:MAX_SUMMARY_CHARS] or None
    except Exception as exc:  # noqa: BLE001
        logger.warning("summarize failed: %s", exc)
        return None


async def _load_batch(session, limit: int) -> list[Memory]:
    stmt = (
        select(Memory)
        .where(Memory.status == MemoryStatus.ACTIVE)
        .where((Memory.summary.is_(None)) | (Memory.summary == ""))
        .order_by(Memory.importance.desc(), Memory.created_at.desc())
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


async def run(*, limit: int | None, dry_run: bool, reembed: bool) -> None:
    embed_text = None
    if reembed:
        from gateway.core.embeddings import embed_text as _embed  # noqa: E402
        embed_text = _embed

    processed = filled = skipped = failed = 0
    batch_size = 200

    async with httpx.AsyncClient() as client:
        sem = asyncio.Semaphore(CONCURRENCY)

        async def worker(mem: Memory) -> tuple[Memory, str | None]:
            content = (mem.content or "").strip()
            if not content:
                return mem, None
            if len(content) <= SHORT_CONTENT_CHARS:
                return mem, _norm(content)[:MAX_SUMMARY_CHARS]
            async with sem:
                return mem, await _summarize(client, content)

        while True:
            remaining = batch_size if limit is None else min(batch_size, limit - processed)
            if remaining <= 0:
                break

            async with async_session() as session:
                memories = await _load_batch(session, remaining)
                if not memories:
                    break

                results = await asyncio.gather(*(worker(m) for m in memories))

                for mem, summary in results:
                    processed += 1
                    if not summary:
                        failed += 1
                        continue
                    if dry_run:
                        filled += 1
                        print(f"[{mem.type:16.16}] {summary}")
                        continue
                    mem.summary = summary
                    if embed_text is not None:
                        try:
                            mem.embedding = embed_text(f"{mem.content}\n{summary}")
                        except Exception:  # noqa: BLE001
                            logger.warning("re-embed failed for %s", mem.id)
                    filled += 1

                if not dry_run:
                    await session.commit()

            logger.info(
                "progress: processed=%d filled=%d failed=%d", processed, filled, failed
            )
            # A short batch means the table is drained.
            if len(memories) < remaining:
                break

    logger.info(
        "DONE processed=%d filled=%d skipped=%d failed=%d%s",
        processed, filled, skipped, failed, " (dry-run)" if dry_run else "",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=None, help="max memories to process")
    parser.add_argument("--dry-run", action="store_true", help="print summaries, don't write")
    parser.add_argument("--reembed", action="store_true", help="refresh embedding with new summary")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(run(limit=args.limit, dry_run=args.dry_run, reembed=args.reembed))


if __name__ == "__main__":
    main()

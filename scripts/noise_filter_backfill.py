#!/usr/bin/env python3
"""Backfill local-model noise review over existing memories (docs/local-noise-filter.md 六).

Pages through HCC's ``/memory/search`` for ``type=tool_result`` (the doc's P0
scope — the much larger ``openclaw_memory``/``openclaw_sync`` bulk-import
bucket, ~2000 rows, is deliberately excluded and needs a separate user
confirmation before it's included, see docs/local-noise-filter.md 零/六),
runs each row through the same review used by the live ``MEMORY_CREATED``
subscriber (``core.local_filter.evaluate``), and either reports the resulting
keep/discard distribution (``--dry-run``, the required first step — never
skip it) or applies it via ``/memory/update``:

- ``keep=false`` -> ``status="discarded"`` (soft delete, recoverable, never a
  hard delete)
- ``keep=true``  -> ``importance`` overwritten with the model's score, so the
  existing retrieval-side ``NOISE_IMPORTANCE_THRESHOLD=0.5`` filter
  (gateway/services/__init__.py) starts treating this row as searchable again

Every reviewed row (dry-run excepted) is tagged ``noise_filter_v1:done`` so
reruns skip it — idempotent, no separate state file, same trick as
``scripts/sync_openclaw_memory.py``.

Usage::

    env -u PYTHONPATH python3 scripts/noise_filter_backfill.py --dry-run
    env -u PYTHONPATH python3 scripts/noise_filter_backfill.py
    env -u PYTHONPATH HCC_URL=http://your-hcc-host:8000 python3 scripts/noise_filter_backfill.py --concurrency 4

The HCC gateway base URL defaults to ``http://localhost:8000``; override via
the ``HCC_URL`` environment variable or ``--hcc-url``.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

from core.local_filter import NOISE_FILTER_TAG, evaluate

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("hcc.noise_filter_backfill")

DEFAULT_HCC_URL = os.environ.get("HCC_URL", "http://localhost:8000")
DEFAULT_TYPE = "tool_result"
DEFAULT_PAGE_SIZE = 50


async def fetch_page(
    client: httpx.AsyncClient, hcc_url: str, type_: str, offset: int, limit: int,
    user_id: str | None, agent_id: str | None,
) -> tuple[list[dict], int]:
    payload: dict = {"query": "", "type": type_, "limit": limit, "offset": offset}
    if user_id:
        payload["user_id"] = user_id
    if agent_id:
        payload["agent_id"] = agent_id
    resp = await client.post(f"{hcc_url}/api/v1/memory/search", json=payload, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    return data["items"], data["total"]


async def review_one(sem: asyncio.Semaphore, item: dict):
    async with sem:
        decision = await evaluate(item["content"], memory_source=item.get("source", ""))
        return item, decision


async def apply_decision(client: httpx.AsyncClient, hcc_url: str, item: dict, decision) -> None:
    tags = list({*(item.get("tags") or []), NOISE_FILTER_TAG})
    payload: dict = {"id": item["id"], "tags": tags}
    if decision.keep:
        payload["importance"] = decision.importance
    else:
        payload["status"] = "discarded"
    resp = await client.post(f"{hcc_url}/api/v1/memory/update", json=payload, timeout=15)
    resp.raise_for_status()


async def main_async(args: argparse.Namespace) -> int:
    sem = asyncio.Semaphore(args.concurrency)
    scanned = reviewed = skipped_done = discarded = kept = errors = 0
    total: int | None = None
    importance_buckets: Counter = Counter()
    decision_source_counts: Counter = Counter()

    async with httpx.AsyncClient() as client:
        offset = 0
        while True:
            items, total = await fetch_page(
                client, args.hcc_url, args.type, offset, args.page_size,
                args.user_id, args.agent_id,
            )
            if not items:
                break
            offset += len(items)
            scanned += len(items)

            pending = [it for it in items if NOISE_FILTER_TAG not in (it.get("tags") or [])]
            skipped_done += len(items) - len(pending)

            if pending:
                results = await asyncio.gather(
                    *(review_one(sem, it) for it in pending), return_exceptions=True
                )
                for res in results:
                    if isinstance(res, Exception):
                        errors += 1
                        logger.warning("review failed: %s", res)
                        continue
                    item, decision = res
                    reviewed += 1
                    decision_source_counts[decision.source] += 1
                    importance_buckets[round(decision.importance, 1)] += 1
                    if decision.keep:
                        kept += 1
                    else:
                        discarded += 1

                    if not args.dry_run:
                        try:
                            await apply_decision(client, args.hcc_url, item, decision)
                        except Exception as exc:
                            errors += 1
                            logger.warning("update failed for id=%s: %s", item["id"], exc)

            logger.info(
                "progress: scanned=%d/%s reviewed=%d discard=%d keep=%d already_done=%d",
                scanned, total, reviewed, discarded, kept, skipped_done,
            )

            if args.max_items and scanned >= args.max_items:
                break

    logger.info("=" * 60)
    logger.info("total matching type=%s: %s", args.type, total)
    logger.info("scanned=%d already_done=%d reviewed=%d errors=%d", scanned, skipped_done, reviewed, errors)
    logger.info("decision: discard=%d keep=%d", discarded, kept)
    logger.info("decision source: %s", dict(decision_source_counts))
    logger.info("importance distribution (keep=true rows use this as their new importance): %s", dict(sorted(importance_buckets.items())))
    if args.dry_run:
        logger.info("[dry-run] no writes performed — rerun without --dry-run to apply")
    return 1 if errors else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--hcc-url", default=DEFAULT_HCC_URL, help="HCC gateway base URL")
    parser.add_argument("--type", default=DEFAULT_TYPE, help="Memory type to scan (default: tool_result)")
    parser.add_argument("--user-id", default=None)
    parser.add_argument("--agent-id", default=None)
    parser.add_argument("--concurrency", type=int, default=4, help="Concurrent Ollama calls (default: 4, ~0.8s/item)")
    parser.add_argument("--page-size", type=int, default=DEFAULT_PAGE_SIZE)
    parser.add_argument("--max-items", type=int, default=None, help="Stop after scanning this many rows (for testing)")
    parser.add_argument("--dry-run", action="store_true", help="Report the keep/discard distribution only, no writes")
    args = parser.parse_args()
    if args.max_items:
        args.page_size = min(args.page_size, args.max_items)

    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Sync OpenClaw's memory index into HCC via the HTTP API.

Reads ``memory_index_chunks`` from OpenClaw's live per-agent sqlite db
(``~/.openclaw/agents/<agent>/agent/openclaw-agent.sqlite`` — NOT the legacy
``~/.openclaw/memory/*.sqlite.migrated`` files, which are pre-migration
archives with no live data) and POSTs each chunk to HCC's
``/api/v1/memory/store``. Runs stdlib + ``requests`` only, no HCC venv
needed, so it can run standalone on the OpenClaw host (aicore).

Dedup / idempotency: OpenClaw indexes the same source file under multiple
path roots (e.g. ``memory/x.md`` and ``HanyanOS/memory/x.md`` both resolve
to the same content), so ``memory_index_chunks.id`` is not a valid dedup key
across runs — it's hashed from (path, content), and path varies. Instead we
hash the chunk *text* alone and tag every synced memory with
``openclaw_hash:<sha256>``. Before syncing, we page through HCC's existing
``agent_id=openclaw`` memories to build the set of already-synced hashes, so
reruns (including a crashed mid-run retry) only ever POST what's missing —
no separate state file to lose or corrupt.

Usage::

    python scripts/sync_openclaw_memory.py --dry-run
    python scripts/sync_openclaw_memory.py
    python scripts/sync_openclaw_memory.py --db /path/to/openclaw-agent.sqlite --hcc-url http://REDACTED-IP:8000
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import sqlite3
import sys
import time
from dataclasses import dataclass

import requests

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("hcc.sync_openclaw_memory")

DEFAULT_DB = "/home/michael/.openclaw/agents/main/agent/openclaw-agent.sqlite"
DEFAULT_HCC_URL = "http://REDACTED-IP:8000"
DEFAULT_AGENT_ID = "openclaw"
DEFAULT_USER_ID = "michael"
SYNC_SOURCE = "openclaw_sync"

# Pure reference/documentation dumps indexed by qmd alongside real memory —
# not memories, just noise (one file alone accounts for ~half the table).
DEFAULT_EXCLUDE_PREFIXES = ("HanyanOS/infra/",)

HASH_TAG_PREFIX = "openclaw_hash:"
ID_TAG_PREFIX = "openclaw_id:"
PATH_TAG_PREFIX = "openclaw_path:"

RETRY_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 2


@dataclass
class Chunk:
    id: str
    path: str
    text: str
    updated_at: int
    content_hash: str


def load_chunks(db_path: str, exclude_prefixes: tuple[str, ...]) -> list[Chunk]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT id, path, text, updated_at FROM memory_index_chunks ORDER BY updated_at ASC"
        ).fetchall()
    finally:
        conn.close()

    seen_hashes: set[str] = set()
    chunks: list[Chunk] = []
    for row_id, path, text, updated_at in rows:
        if any(path.startswith(p) for p in exclude_prefixes):
            continue
        content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if content_hash in seen_hashes:
            continue  # same content indexed under a second path root
        seen_hashes.add(content_hash)
        chunks.append(Chunk(id=row_id, path=path, text=text, updated_at=updated_at, content_hash=content_hash))
    return chunks


def fetch_existing_hashes(session: requests.Session, hcc_url: str, agent_id: str) -> set[str]:
    existing: set[str] = set()
    offset = 0
    limit = 100
    while True:
        resp = session.post(
            f"{hcc_url}/api/v1/memory/search",
            json={"query": "", "agent_id": agent_id, "limit": limit, "offset": offset},
            timeout=15,
        )
        resp.raise_for_status()
        items = resp.json().get("items", [])
        if not items:
            break
        for item in items:
            for tag in item.get("tags") or []:
                if isinstance(tag, str) and tag.startswith(HASH_TAG_PREFIX):
                    existing.add(tag[len(HASH_TAG_PREFIX):])
        if len(items) < limit:
            break
        offset += limit
    return existing


def store_chunk(
    session: requests.Session,
    hcc_url: str,
    chunk: Chunk,
    *,
    user_id: str,
    agent_id: str,
) -> bool:
    payload = {
        "user_id": user_id,
        "agent_id": agent_id,
        "type": "openclaw_memory",
        "content": chunk.text,
        "summary": "",
        "importance": 0.5,
        "tags": [
            f"source:{SYNC_SOURCE}",
            f"{HASH_TAG_PREFIX}{chunk.content_hash}",
            f"{ID_TAG_PREFIX}{chunk.id}",
            f"{PATH_TAG_PREFIX}{chunk.path}",
        ],
        "source": SYNC_SOURCE,
    }
    last_error: Exception | None = None
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            resp = session.post(f"{hcc_url}/api/v1/memory/store", json=payload, timeout=15)
            resp.raise_for_status()
            return True
        except requests.RequestException as exc:  # noqa: PERF203 - retry loop is the point
            last_error = exc
            if attempt < RETRY_ATTEMPTS:
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)
    logger.error("failed to store chunk id=%s path=%s after %d attempts: %s", chunk.id, chunk.path, RETRY_ATTEMPTS, last_error)
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=DEFAULT_DB, help="Path to OpenClaw's openclaw-agent.sqlite")
    parser.add_argument("--hcc-url", default=DEFAULT_HCC_URL, help="HCC gateway base URL")
    parser.add_argument("--agent-id", default=DEFAULT_AGENT_ID)
    parser.add_argument("--user-id", default=DEFAULT_USER_ID)
    parser.add_argument(
        "--exclude-prefix", action="append", dest="exclude_prefixes", metavar="PREFIX",
        help="Path prefix to skip (repeatable). Defaults to %r." % (DEFAULT_EXCLUDE_PREFIXES,),
    )
    parser.add_argument("--dry-run", action="store_true", help="Report what would sync without writing")
    args = parser.parse_args()

    exclude_prefixes = tuple(args.exclude_prefixes) if args.exclude_prefixes else DEFAULT_EXCLUDE_PREFIXES

    chunks = load_chunks(args.db, exclude_prefixes)
    logger.info("loaded %d deduped chunks from %s (excluded prefixes: %s)", len(chunks), args.db, exclude_prefixes)

    session = requests.Session()
    existing_hashes = fetch_existing_hashes(session, args.hcc_url, args.agent_id)
    logger.info("found %d already-synced chunks in HCC (agent_id=%s)", len(existing_hashes), args.agent_id)

    to_sync = [c for c in chunks if c.content_hash not in existing_hashes]
    logger.info("%d chunk(s) new since last sync", len(to_sync))

    if args.dry_run:
        for chunk in to_sync[:20]:
            logger.info("  [dry-run] would store path=%s id=%s", chunk.path, chunk.id)
        if len(to_sync) > 20:
            logger.info("  [dry-run] ... and %d more", len(to_sync) - 20)
        return 0

    stored = failed = 0
    for chunk in to_sync:
        if store_chunk(session, args.hcc_url, chunk, user_id=args.user_id, agent_id=args.agent_id):
            stored += 1
        else:
            failed += 1

    logger.info("sync complete: stored=%d failed=%d skipped_existing=%d", stored, failed, len(chunks) - len(to_sync))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

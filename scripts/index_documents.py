#!/usr/bin/env python3
"""Index markdown collections into HCC's ``documents`` table.

Replaces QMD's file indexing for document retrieval: walks one or more
collection roots, hashes each file, and upserts changed files into Postgres
via :class:`gateway.services.document_service.DocumentService`. Files whose
content hash is unchanged since the last run are skipped (no re-tokenize, no
re-embed); files that disappeared from disk since the last run are deleted
from their collection.

Run from the repo root (so ``gateway.core.config`` picks up ``.env``) with the
project venv active::

    python scripts/index_documents.py
    python scripts/index_documents.py --collection second-brain --root ~/workspace/AICore/含烟记忆系统
    python scripts/index_documents.py --embed   # also compute embeddings (see caveat below)

Embeddings are OFF by default. HCC has no real embedding model available
server-side (see ``mcp/memory_tools.py`` for why) — ``gateway.core.embeddings``
falls back to a hash placeholder that is a different, incompatible vector
space from real Qwen3-Embedding vectors. Passing ``--embed`` only computes
anything when ``HCC_EMBEDDING_PROVIDER`` is set to something other than
``hash``; otherwise it's a no-op and a warning is printed once. BM25 (jieba +
tsvector) works unconditionally either way.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scanner.parser import parse_file  # noqa: E402
from gateway.core.database import async_session  # noqa: E402
from gateway.services.document_service import DocumentService  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("hcc.index_documents")

EXCLUDE_DIRS = {".git", "node_modules", "__pycache__", ".venv", ".obsidian", "dist-info"}

DEFAULT_COLLECTIONS = {
    "second-brain": "~/workspace/AICore/含烟记忆系统",
    # QMD's own dev-brain collection points at ~/projects/dev-vault, which
    # doesn't exist on disk (0 files indexed there per `qmd status`) — the
    # real vault lives here, so HCC indexes the actual content instead of
    # replicating the stale QMD config.
    "dev-brain": "~/workspace/projects/dev-vault",
}


def iter_markdown_files(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
        for name in filenames:
            if name.lower().endswith(".md"):
                yield Path(dirpath) / name


async def index_collection(collection: str, root: Path, *, embed: bool, embed_warned: list[bool]) -> dict:
    if not root.exists():
        logger.warning("[%s] root does not exist, skipping: %s", collection, root)
        return {"collection": collection, "found": 0, "changed": 0, "skipped": 0, "deleted": 0, "errors": 0}

    found = changed = skipped = errors = 0
    keep_paths: set[str] = set()

    async with async_session() as session:
        service = DocumentService(session)

        for path in iter_markdown_files(root):
            found += 1
            rel = path.relative_to(root).as_posix()
            keep_paths.add(rel)
            try:
                raw = path.read_bytes()
                content_hash = hashlib.sha256(raw).hexdigest()
                doc = parse_file(path)
                mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).replace(tzinfo=None)

                embedding = None
                if embed:
                    from gateway.core.embeddings import embed_text, EMBEDDING_PROVIDER

                    if EMBEDDING_PROVIDER == "hash":
                        if not embed_warned[0]:
                            logger.warning(
                                "--embed given but HCC_EMBEDDING_PROVIDER=hash (placeholder, "
                                "incompatible vector space) — skipping embeddings entirely"
                            )
                            embed_warned[0] = True
                    else:
                        embedding = embed_text(f"{doc.title}\n{doc.content}")

                _, was_changed = await service.upsert(
                    collection=collection,
                    path=rel,
                    title=doc.title,
                    content=doc.content,
                    content_hash=content_hash,
                    mtime=mtime,
                    embedding=embedding,
                )
                if was_changed:
                    changed += 1
                else:
                    skipped += 1
            except Exception:  # noqa: BLE001 - one bad file must not abort the run
                errors += 1
                logger.exception("[%s] failed to index %s", collection, path)

        await session.commit()

        deleted = await service.delete_missing(collection, keep_paths)
        await session.commit()

    result = {
        "collection": collection,
        "found": found,
        "changed": changed,
        "skipped": skipped,
        "deleted": deleted,
        "errors": errors,
    }
    logger.info(
        "[%s] found=%d changed=%d skipped=%d deleted=%d errors=%d",
        collection, found, changed, skipped, deleted, errors,
    )
    return result


async def main_async(collections: dict[str, str], embed: bool) -> int:
    embed_warned = [False]
    results = []
    for collection, root_str in collections.items():
        root = Path(root_str).expanduser()
        results.append(await index_collection(collection, root, embed=embed, embed_warned=embed_warned))

    total_errors = sum(r["errors"] for r in results)
    return 1 if total_errors else 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--collection", action="append", dest="collections", metavar="NAME",
        help="Collection name to index (repeatable). Defaults to both second-brain and dev-brain.",
    )
    parser.add_argument(
        "--root", action="append", dest="roots", metavar="PATH",
        help="Root path for the preceding --collection (must pair 1:1 with --collection).",
    )
    parser.add_argument("--embed", action="store_true", help="Also compute embeddings (see module docstring caveat).")
    args = parser.parse_args()

    if args.collections:
        if not args.roots or len(args.roots) != len(args.collections):
            parser.error("--root must be given once per --collection, in the same order")
        collections = dict(zip(args.collections, args.roots))
    else:
        collections = DEFAULT_COLLECTIONS

    exit_code = asyncio.run(main_async(collections, args.embed))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()

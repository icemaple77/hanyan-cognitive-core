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

    python scripts/index_documents.py                       # full AICore vault (default)
    python scripts/index_documents.py --collection second-brain --root ~/workspace/AICore/含烟记忆系统
    python scripts/index_documents.py --embed   # also compute embeddings (see caveat below)

Default root is the whole AICore vault (``~/workspace/AICore``), not just the
含烟记忆系统 subtree — see docs/dreaming-design.md-adjacent Obsidian export
notes. ``EXCLUDE_DIRS`` keeps Archive/backup/Obsidian-internal directories out
of the index (physically-external archive material and full-vault backups
have no business being retrieval candidates).

Embeddings are OFF by default (``--embed`` opt-in) even though the server can
now compute real ones (``gateway.core.embeddings``, HCC_EMBEDDING_PROVIDER=
ollama as of 2026-08 — see 体检报告 P0-1) — indexing thousands of files is a
lot of extra ollama calls for a one-off batch job, so it stays explicit. If
``HCC_EMBEDDING_PROVIDER`` is still ``hash`` (lexical placeholder, not real
semantic search), ``--embed`` is a no-op with a one-time warning rather than
silently writing incompatible vectors. BM25 (jieba + tsvector) works
unconditionally either way.

PDF files (``*.pdf``) are indexed alongside markdown by default, via
``pdf_inspector`` (see ``scanner.parser.parse_pdf_file``). PDFs have no
frontmatter, so their extracted markdown becomes ``content`` directly and
their title comes from PDF metadata (falling back to the filename). PDFs
with no extractable text layer (scanned/image-based, per pdf_inspector's
classification) are skipped and counted separately — never treated as
errors. Set ``HCC_INDEX_PDF=0`` to disable PDF indexing entirely.
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

from scanner.parser import parse_file, parse_pdf_file, PdfSkipped
from gateway.core.database import async_session
from gateway.services.document_service import DocumentService

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("hcc.index_documents")

# 遍历规则与集合定义的**唯一来源**在 core/doc_index.py —— 网关的增量索引循环
# (core.doc_index.DocIndexSync)与本脚本必须用同一套规则,分开写迟早会漂
# (2026-09-03 的向量事故就是同一个知识有两个定义造成的)。
from core.doc_index import (  # noqa: E402
    DEFAULT_COLLECTIONS,
    file_mtime_utc,
    EXCLUDE_DIRS,
    INDEX_PDF,
    iter_indexable_files,
)

async def index_collection(collection: str, root: Path, *, embed: bool, embed_warned: list[bool]) -> dict:
    if not root.exists():
        logger.warning("[%s] root does not exist, skipping: %s", collection, root)
        return {
            "collection": collection, "found": 0, "changed": 0, "skipped": 0,
            "skipped_pdf": 0, "deleted": 0, "errors": 0,
        }

    found = changed = skipped = skipped_pdf = errors = 0
    keep_paths: set[str] = set()

    async with async_session() as session:
        service = DocumentService(session)

        for path in iter_indexable_files(root):
            found += 1
            rel = path.relative_to(root).as_posix()
            keep_paths.add(rel)
            try:
                raw = path.read_bytes()
                content_hash = hashlib.sha256(raw).hexdigest()

                if path.suffix.lower() == ".pdf":
                    try:
                        doc = parse_pdf_file(path)
                    except PdfSkipped as exc:
                        skipped_pdf += 1
                        logger.info("[%s] skipping %s: %s", collection, rel, exc)
                        continue
                else:
                    doc = parse_file(path)

                mtime = file_mtime_utc(path)  # 与增量循环共用同一定义

                embedding = None
                if embed:
                    from gateway.core.embeddings import embed_text, document_embedding_text, EMBEDDING_PROVIDER

                    if EMBEDDING_PROVIDER == "hash":
                        if not embed_warned[0]:
                            logger.warning(
                                "--embed given but HCC_EMBEDDING_PROVIDER=hash (placeholder, "
                                "incompatible vector space) — skipping embeddings entirely"
                            )
                            embed_warned[0] = True
                    else:
                        embedding = embed_text(document_embedding_text(doc.title, doc.content))

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
            except Exception:
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
        "skipped_pdf": skipped_pdf,
        "deleted": deleted,
        "errors": errors,
    }
    logger.info(
        "[%s] found=%d changed=%d skipped=%d skipped_pdf=%d deleted=%d errors=%d",
        collection, found, changed, skipped, skipped_pdf, deleted, errors,
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

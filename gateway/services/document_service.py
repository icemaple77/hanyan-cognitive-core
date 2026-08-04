"""Document indexing + hybrid search — the QMD-replacement doc store.

Mirrors :class:`gateway.services.MemoryService`'s BM25/vector/RRF pipeline
(same tokenizer, same RRF fusion, same optional reranker) but over
:class:`gateway.models.Document` instead of ``Memory``. Kept as a separate
service rather than folded into ``MemoryService`` because documents have a
different identity (``collection`` + ``path``, deduped by content hash) and
no ownership/importance/reinforcement semantics — conflating the two would
mean bolting doc-only columns onto every memory row.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.core.fts import tokenize_for_fts
from gateway.core.rerank import RERANK_ENABLED, rerank as rerank_fn
from gateway.core.rrf import reciprocal_rank_fusion
from gateway.models import Document

SNIPPET_DEFAULT_LEN = 240


def make_snippet(content: str, query: str, length: int = SNIPPET_DEFAULT_LEN) -> str:
    """Return an excerpt of ``content`` centered on the first query-token hit.

    Falls back to the head of the document when the query is empty or none
    of its tokens appear verbatim (e.g. a pure-vector match) — a snippet is
    always returned, never an empty string, so callers can render it as-is.
    """
    content = content or ""
    if not content:
        return ""

    needle_pos = -1
    if query:
        # Look for the raw query and, failing that, each jieba token — the
        # tokenizer used for indexing may split words the raw substring
        # search would miss (e.g. "环境清单" as a whole vs. "环境"/"清单").
        candidates = [query.strip()] + tokenize_for_fts(query).split()
        lowered = content.lower()
        for cand in candidates:
            cand = cand.strip()
            if not cand:
                continue
            pos = lowered.find(cand.lower())
            if pos != -1:
                needle_pos = pos
                break

    if needle_pos == -1:
        excerpt = content[:length]
    else:
        start = max(0, needle_pos - length // 3)
        excerpt = content[start : start + length]

    excerpt = excerpt.strip()
    if len(excerpt) < len(content.strip()):
        excerpt += "…"
    return excerpt


class DocumentService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def upsert(
        self,
        *,
        collection: str,
        path: str,
        title: str,
        content: str,
        content_hash: str,
        mtime=None,
        embedding: Optional[list[float]] = None,
    ) -> tuple[Document, bool]:
        """Insert or update the document identified by ``(collection, path)``.

        Returns ``(document, changed)`` — ``changed`` is ``False`` when the
        stored ``content_hash`` already matches (nothing written), so callers
        (the indexer) can report skip counts without a second query.
        """
        stmt = select(Document).where(Document.collection == collection, Document.path == path)
        existing = (await self.session.execute(stmt)).scalar_one_or_none()

        if existing is not None and existing.content_hash == content_hash:
            return existing, False

        if existing is None:
            doc = Document(
                collection=collection,
                path=path,
                title=title,
                content=content,
                content_hash=content_hash,
                mtime=mtime,
                embedding=embedding,
            )
            self.session.add(doc)
        else:
            doc = existing
            doc.title = title
            doc.content = content
            doc.content_hash = content_hash
            doc.mtime = mtime
            if embedding is not None:
                doc.embedding = embedding

        await self.session.flush()
        return doc, True

    async def delete_missing(self, collection: str, keep_paths: set[str]) -> int:
        """Delete rows for ``collection`` whose path is no longer on disk."""
        stmt = select(Document).where(Document.collection == collection)
        rows = (await self.session.execute(stmt)).scalars().all()
        removed = 0
        for row in rows:
            if row.path not in keep_paths:
                await self.session.delete(row)
                removed += 1
        if removed:
            await self.session.flush()
        return removed

    async def keyword_search_bm25(
        self,
        query: str,
        limit: int = 20,
        collection: Optional[str] = None,
    ) -> list[tuple[Document, float]]:
        """BM25-style full-text search via Postgres ``ts_rank_cd``.

        See :meth:`gateway.services.MemoryService.keyword_search_bm25` — same
        tokenizer, same ``plainto_tsquery`` choice, same rationale.
        """
        tokens = tokenize_for_fts(query)
        if not tokens:
            return []

        tsvector_expr = func.to_tsvector("simple", Document.search_text)
        tsquery_expr = func.plainto_tsquery("simple", tokens)
        rank = func.ts_rank_cd(tsvector_expr, tsquery_expr).label("rank")

        stmt = select(Document, rank).where(tsvector_expr.op("@@")(tsquery_expr))
        if collection:
            stmt = stmt.where(Document.collection == collection)
        stmt = stmt.order_by(rank.desc()).limit(limit)

        result = await self.session.execute(stmt)
        return [(doc, float(r)) for doc, r in result.all()]

    async def semantic_search(
        self,
        embedding: list[float],
        limit: int = 10,
        collection: Optional[str] = None,
    ) -> list[tuple[Document, float]]:
        """Return the ``limit`` documents most similar to ``embedding`` (cosine)."""
        distance = Document.embedding.cosine_distance(embedding).label("distance")

        stmt = select(Document, distance).where(Document.embedding.is_not(None))
        if collection:
            stmt = stmt.where(Document.collection == collection)
        stmt = stmt.order_by(distance).limit(limit)

        result = await self.session.execute(stmt)
        return [(doc, float(dist)) for doc, dist in result.all()]

    async def hybrid_search(
        self,
        query: str = "",
        embedding: Optional[list[float]] = None,
        limit: int = 10,
        collection: Optional[str] = None,
        candidate_pool: int = 50,
        rerank: bool = False,
        snippet_length: int = SNIPPET_DEFAULT_LEN,
    ) -> list[dict]:
        """BM25 + vector hybrid search over documents, fused with RRF.

        Same shape/semantics as :meth:`MemoryService.hybrid_search` — either
        ``query`` or ``embedding`` may be omitted, RRF fuses whichever
        branch(es) ran, and ``rerank=True`` only takes effect if the server
        also has ``HCC_RERANK_ENABLED=true``. Each result additionally
        carries a ``snippet`` excerpt (see :func:`make_snippet`).
        """
        bm25_results: list[tuple[Document, float]] = []
        vector_results: list[tuple[Document, float]] = []

        if query:
            bm25_results = await self.keyword_search_bm25(query, limit=candidate_pool, collection=collection)
        if embedding:
            vector_results = await self.semantic_search(embedding, limit=candidate_pool, collection=collection)

        fused = reciprocal_rank_fusion(bm25_results, vector_results)
        for item in fused:
            item["document"] = item.pop("row")
        do_rerank = rerank and RERANK_ENABLED
        top = fused[: max(limit, candidate_pool) if do_rerank else limit]

        if do_rerank and top:
            scores = await rerank_fn(query or "", [item["document"].content for item in top])
            if scores is not None:
                for item, score in zip(top, scores):
                    item["rerank_score"] = score
                top.sort(key=lambda item: item["rerank_score"], reverse=True)

        top = top[:limit]
        for item in top:
            item["snippet"] = make_snippet(item["document"].content, query, snippet_length)
        return top

    async def list_recent(
        self, limit: int = 20, offset: int = 0, collection: Optional[str] = None
    ) -> tuple[list[Document], int]:
        stmt = select(Document).order_by(Document.updated_at.desc()).offset(offset).limit(limit)
        count_stmt = select(func.count(Document.id))
        if collection:
            stmt = stmt.where(Document.collection == collection)
            count_stmt = count_stmt.where(Document.collection == collection)

        total = (await self.session.execute(count_stmt)).scalar() or 0
        rows = (await self.session.execute(stmt)).scalars().all()
        return list(rows), total

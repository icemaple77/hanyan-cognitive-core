"""Document search routes — the QMD-replacement doc retrieval API."""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.core.database import get_session
from gateway.schemas.document import (
    DocumentSearch,
    DocumentListResponse,
    DocumentResponse,
    DocumentHybridSearchRequest,
    DocumentHybridSearchItem,
    DocumentHybridSearchResponse,
)
from gateway.services.document_service import DocumentService

router = APIRouter()


@router.post("/document/search", response_model=DocumentListResponse)
async def search_documents(
    query: DocumentSearch,
    session: AsyncSession = Depends(get_session),
) -> DocumentListResponse:
    """Plain BM25 keyword search over indexed documents (no vector/RRF)."""
    service = DocumentService(session)
    results = await service.keyword_search_bm25(query.query, limit=query.limit, collection=query.collection)
    return DocumentListResponse(
        items=[DocumentResponse.model_validate(doc) for doc, _ in results],
        total=len(results),
    )


@router.post("/document/hybrid-search", response_model=DocumentHybridSearchResponse)
async def hybrid_search_documents(
    query: DocumentHybridSearchRequest,
    session: AsyncSession = Depends(get_session),
) -> DocumentHybridSearchResponse:
    """BM25 + vector hybrid search over documents (QMD-style), fused with RRF.

    Returns each hit's document (path + full content) alongside a ``snippet``
    excerpt centered on the query match, mirroring what ``qmd query`` used to
    return but served from HCC's own PG-backed pipeline.
    """
    service = DocumentService(session)
    results = await service.hybrid_search(
        query=query.query,
        embedding=query.embedding,
        limit=query.limit,
        collection=query.collection,
        candidate_pool=query.candidate_pool,
        rerank=query.rerank,
        snippet_length=query.snippet_length,
    )
    items = [
        DocumentHybridSearchItem(
            document=DocumentResponse.model_validate(item["document"]),
            snippet=item["snippet"],
            rrf_score=item["rrf_score"],
            bm25_rank=item.get("bm25_rank"),
            bm25_score=item.get("bm25_score"),
            vector_rank=item.get("vector_rank"),
            vector_distance=item.get("vector_distance"),
            rerank_score=item.get("rerank_score"),
        )
        for item in results
    ]
    return DocumentHybridSearchResponse(items=items, total=len(items))


@router.get("/document/recent", response_model=DocumentListResponse)
async def recent_documents(
    limit: int = 20,
    offset: int = 0,
    collection: str | None = None,
    session: AsyncSession = Depends(get_session),
) -> DocumentListResponse:
    service = DocumentService(session)
    docs, total = await service.list_recent(limit=limit, offset=offset, collection=collection)
    return DocumentListResponse(
        items=[DocumentResponse.model_validate(d) for d in docs],
        total=total,
    )

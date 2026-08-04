"""Pydantic schemas for Memory API."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class MemoryCreate(BaseModel):
    user_id: str = Field(..., description="User identifier")
    agent_id: str = "default"
    shared: bool = False
    type: str = "general"
    content: str = Field(..., min_length=1)
    summary: str = ""
    importance: float = Field(default=0.5, ge=0.0, le=1.0)
    tags: list[str] = Field(default_factory=list)
    source: str = "api"
    embedding: list[float] | None = Field(default=None, description="预计算好的向量,客户端算好传入")


class MemoryUpdate(BaseModel):
    id: str
    content: str | None = None
    summary: str | None = None
    importance: float | None = Field(None, ge=0.0, le=1.0)
    tags: list[str] | None = None
    status: str | None = None
    embedding: list[float] | None = None


class MemorySearch(BaseModel):
    query: str = ""
    user_id: str | None = None
    agent_id: str | None = None
    shared: bool | None = None
    type: str | None = None
    limit: int = Field(default=20, ge=1, le=100)
    offset: int = Field(default=0, ge=0)


class MemoryResponse(BaseModel):
    id: str
    user_id: str
    agent_id: str
    shared: bool
    type: str
    content: str
    summary: str
    importance: float
    tags: list[Any]
    source: str
    status: str
    access_count: int = 0
    last_access: datetime | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class MemoryListResponse(BaseModel):
    items: list[MemoryResponse]
    total: int


class SemanticSearchRequest(BaseModel):
    embedding: list[float] = Field(..., description="Vector embedding to search by")
    limit: int = Field(default=10, ge=1, le=100)
    user_id: str | None = None
    agent_id: str | None = None
    type: str | None = None


class HybridSearchRequest(BaseModel):
    query: str = Field(default="", description="Free-text query for BM25 full-text search")
    embedding: list[float] | None = Field(
        default=None, description="Optional precomputed vector for the semantic branch; client-computed, same as /memory/semantic-search"
    )
    limit: int = Field(default=10, ge=1, le=100)
    user_id: str | None = None
    agent_id: str | None = None
    type: str | None = None
    candidate_pool: int = Field(
        default=50, ge=1, le=200, description="How many results each of BM25/vector contributes before RRF fusion"
    )
    rerank: bool = Field(
        default=False, description="Rerank the fused top results with the optional cross-encoder (HCC_RERANK_ENABLED must also be on; silently falls back to RRF order if unavailable)"
    )


class HybridSearchItem(BaseModel):
    memory: MemoryResponse
    rrf_score: float
    bm25_rank: int | None = None
    bm25_score: float | None = None
    vector_rank: int | None = None
    vector_distance: float | None = None
    rerank_score: float | None = None


class HybridSearchResponse(BaseModel):
    items: list[HybridSearchItem]
    total: int

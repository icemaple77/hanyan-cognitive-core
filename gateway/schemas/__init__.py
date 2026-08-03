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

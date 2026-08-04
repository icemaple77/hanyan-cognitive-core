"""Memory SQLAlchemy model."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, String, Float, DateTime, Text, JSON, Boolean, Integer, Index, event, text
from pgvector.sqlalchemy import Vector

from gateway.core.database import Base
from gateway.core.fts import build_search_text

# Dimensionality of the stored embeddings. 768 matches common sentence-embedding
# models (e.g. all-mpnet-base-v2, nomic-embed-text). Keep in sync with the value
# used by the embedding provider in gateway.core.embeddings.
EMBEDDING_DIM = 1024  # Qwen3-Embedding-0.6B 原生维度(2026-08 接入,0条历史数据用旧768维,无迁移负担)


class Memory(Base):
    __tablename__ = "memories"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(128), index=True, nullable=False)
    agent_id = Column(String(64), index=True, default="default", nullable=False)
    shared = Column(Boolean, default=False)
    type = Column(String(64), default="general", index=True)
    content = Column(Text, nullable=False)
    summary = Column(Text, default="")
    importance = Column(Float, default=0.5)
    tags = Column(JSON, default=list)
    source = Column(String(64), default="api")
    status = Column(String(32), default="active")
    embedding = Column(Vector(EMBEDDING_DIM), nullable=True)
    # Pre-tokenized (jieba, see gateway.core.fts) blob of content+summary+tags,
    # kept in sync by the before_insert/before_update listeners below. Indexed
    # via a GIN expression index (to_tsvector('simple', search_text)) for BM25
    # ranking — 'simple' just lowercases/splits, all the real CJK segmentation
    # already happened in Python so both index and query tokenize identically.
    search_text = Column(Text, default="", nullable=False, server_default="")
    access_count = Column(Integer, default=0, nullable=False)
    last_access = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None), onupdate=lambda: datetime.now(timezone.utc).replace(tzinfo=None))

    __table_args__ = (
        Index(
            "ix_memories_search_text_fts",
            text("to_tsvector('simple', search_text)"),
            postgresql_using="gin",
        ),
    )


@event.listens_for(Memory, "before_insert")
@event.listens_for(Memory, "before_update")
def _sync_search_text(mapper, connection, target: "Memory") -> None:
    target.search_text = build_search_text(target.content, target.summary, target.tags)

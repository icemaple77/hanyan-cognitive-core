"""Memory SQLAlchemy model."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, String, Float, DateTime, Text, JSON, Boolean
from pgvector.sqlalchemy import Vector

from gateway.core.database import Base

# Dimensionality of the stored embeddings. 768 matches common sentence-embedding
# models (e.g. all-mpnet-base-v2, nomic-embed-text). Keep in sync with the value
# used by the embedding provider in gateway.core.embeddings.
EMBEDDING_DIM = 768


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
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None), onupdate=lambda: datetime.now(timezone.utc).replace(tzinfo=None))

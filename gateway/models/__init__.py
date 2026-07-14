"""Memory SQLAlchemy model."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, String, Float, DateTime, Text, JSON
from gateway.core.database import Base


class Memory(Base):
    __tablename__ = "memories"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(128), index=True, nullable=False)
    type = Column(String(64), default="general", index=True)
    content = Column(Text, nullable=False)
    summary = Column(Text, default="")
    importance = Column(Float, default=0.5)
    tags = Column(JSON, default=list)
    source = Column(String(64), default="api")
    status = Column(String(32), default="active")
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

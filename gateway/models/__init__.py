"""Memory + Document SQLAlchemy models."""

import uuid
from datetime import datetime, timezone
from enum import StrEnum

from sqlalchemy import Column, String, Float, Date, DateTime, Text, JSON, Boolean, Integer, Index, UniqueConstraint, CheckConstraint, event, text
from pgvector.sqlalchemy import Vector

from gateway.core.database import Base
from gateway.core.fts import build_search_text, tokenize_for_fts

# Dimensionality of the stored embeddings. Keep in sync with HCC_EMBEDDING_DIM
# (gateway.core.embeddings) and whatever HCC_EMBEDDING_MODEL actually produces.
EMBEDDING_DIM = 768  # nomic-embed-text 原生维度(2026-08 切到 ollama 服务端计算,0条历史数据,无迁移负担)


class MemoryStatus(StrEnum):
    """Canonical values for ``Memory.status`` (soft-delete lifecycle states).

    Two distinct soft-delete semantics share this column, kept separate
    rather than merged (see docs/local-noise-filter.md 零):
    - DISCARDED: local-model noise filter verdict (keep=false) — a memory
      judged low-quality/noise at write time.
    - ARCHIVED: forget-engine decay or explicit delete — a memory that faded
      or was intentionally removed after having been useful.
    Every read-path query filters ``status == ACTIVE`` (whitelist), so both
    DISCARDED and ARCHIVED are already excluded from retrieval identically.
    """

    ACTIVE = "active"
    DISCARDED = "discarded"
    ARCHIVED = "archived"


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
    status = Column(String(32), default=MemoryStatus.ACTIVE.value)
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
        CheckConstraint(
            "status IN ('active', 'discarded', 'archived')",
            name="ck_memories_status",
        ),
    )


@event.listens_for(Memory, "before_insert")
@event.listens_for(Memory, "before_update")
def _sync_search_text(mapper, connection, target: "Memory") -> None:
    target.search_text = build_search_text(target.content, target.summary, target.tags)


class Document(Base):
    """Indexed markdown document (QMD-replacement doc store).

    One row per source file — collection (e.g. ``second-brain``, ``dev-brain``)
    plus a path relative to that collection's root. ``content_hash`` (sha256 of
    the raw file bytes) lets the indexer skip re-embedding/re-tokenizing files
    that haven't changed since the last run. Search reuses the exact same
    BM25 (jieba + tsvector) / vector / RRF pipeline as :class:`Memory` — see
    :mod:`gateway.services.document_service`.
    """

    __tablename__ = "documents"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    collection = Column(String(64), index=True, nullable=False)
    path = Column(Text, nullable=False)
    title = Column(Text, default="")
    content = Column(Text, nullable=False)
    content_hash = Column(String(64), nullable=False)
    # Same jieba-pretokenized blob approach as Memory.search_text — kept in
    # sync by the before_insert/before_update listener below.
    search_text = Column(Text, default="", nullable=False, server_default="")
    embedding = Column(Vector(EMBEDDING_DIM), nullable=True)
    mtime = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None), onupdate=lambda: datetime.now(timezone.utc).replace(tzinfo=None))

    __table_args__ = (
        UniqueConstraint("collection", "path", name="uq_documents_collection_path"),
        Index(
            "ix_documents_search_text_fts",
            text("to_tsvector('simple', search_text)"),
            postgresql_using="gin",
        ),
    )


@event.listens_for(Document, "before_insert")
@event.listens_for(Document, "before_update")
def _sync_document_search_text(mapper, connection, target: "Document") -> None:
    target.search_text = tokenize_for_fts(f"{target.title or ''}\n{target.content or ''}")


class DreamSignal(Base):
    """Light/REM phase hits — auxiliary boost record, never touches Memory.

    See docs/dreaming-design.md 2.3. The (memory_id, phase, day) unique index
    is the idempotency mechanism: re-running Light/REM within the same day for
    a memory that already has a signal is a no-op at the DB layer, not just an
    application-level check.
    """

    __tablename__ = "dream_signals"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    memory_id = Column(String(36), nullable=False, index=True)
    phase = Column(String(16), nullable=False)  # light | rem
    boost = Column(Float, nullable=False)
    cluster_tag = Column(Text, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None), nullable=False)

    __table_args__ = (
        Index(
            "uq_dream_signals_memory_phase_day",
            "memory_id",
            "phase",
            text("DATE(created_at)"),
            unique=True,
        ),
    )


class EmotionSnapshot(Base):
    """Deep-phase daily cold snapshot of the emotion state (docs/emotion-design.md 2.6).

    One row per calendar day (``snapshot_date`` unique) — day-grain is
    deliberate: current state is served hot from Redis, this table only backs
    long-term trend / "回顾某天心情" queries, so a high-frequency write table
    isn't warranted (see the design doc's rationale in 2.6).
    """

    __tablename__ = "emotion_snapshots"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    snapshot_date = Column(Date, nullable=False, unique=True)
    state = Column(JSON, nullable=False)
    named_state = Column(String(32), nullable=True)
    dominant_trigger = Column(Text, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None), nullable=False)


class DreamRun(Base):
    """Audit record for one Light/REM/Deep pass — feeds /dream/status and the
    "did we already run today" idempotency guard that fixes the double-write
    ("今夜无梦" x3) bug (see docs/dreaming-design.md 零 + 2.6)."""

    __tablename__ = "dream_runs"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    phase = Column(String(16), nullable=False)  # light | rem | deep
    started_at = Column(DateTime, nullable=False)
    finished_at = Column(DateTime, nullable=True)
    stats = Column(JSON, nullable=False, default=dict)
    narrative_path = Column(Text, nullable=True)

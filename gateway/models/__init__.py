"""Memory + Document SQLAlchemy models."""

import uuid
from datetime import datetime, timezone
from enum import StrEnum

from sqlalchemy import Column, String, Float, Date, DateTime, Text, JSON, Boolean, Integer, Index, UniqueConstraint, CheckConstraint, event, text
from pgvector.sqlalchemy import Vector

from gateway.core.database import Base
from gateway.core.fts import build_search_text, tokenize_for_fts

# 建表用的向量维度 —— **从单一配置源读,禁止在此硬编码**。
# 2026-09-03 事故:这里曾硬编码 1024 而运行时实际产出 768(.env),导致 documents
# 列 1024/查询 768,语义检索每次报 "different vector dimensions"、知识召回静默
# 降级成纯 BM25。建表维度与产出维度自此共用 core_settings.embedding_dim。
from core.config import core_settings

EMBEDDING_DIM = core_settings.embedding_dim


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
    # 这条向量是**哪个模型**算的(向量空间身份)。2026-08-29 换模型时靠人肉记
    # "我是不是全跑了",结果漏了 documents 整张表、5 天没人知道。有了这一列,
    # "还有哪些行没重算" 是一条 SQL 查得出来的事实,不是猜。见 scripts/reembed_all.py。
    embedding_model = Column(String(128), nullable=True, index=True)
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
    # 同 Memory.embedding_model:向量空间身份,换模型时用来查"谁还没重算"。
    embedding_model = Column(String(128), nullable=True, index=True)
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


class MemoryConflict(Base):
    """Audit trail for the P1-3 write-path staleness check — one row per
    (old, new) pair the check in ``gateway.services._flag_stale_duplicates``
    flags as same-topic/likely-superseded.

    Satellite table, same pattern as :class:`DreamSignal`: it never mutates
    the old ``Memory`` row beyond the ``stale`` tag already applied by the
    caller, it just records *that* the flag happened (and why — the cosine
    distance) so "what got superseded recently" is a query instead of a scan
    over every memory's tags.
    """

    __tablename__ = "memory_conflicts"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    old_memory_id = Column(String(36), nullable=False, index=True)
    new_memory_id = Column(String(36), nullable=False, index=True)
    distance = Column(Float, nullable=False)
    user_id = Column(String(128), index=True, nullable=False)
    agent_id = Column(String(64), index=True, default="default")
    type = Column(String(64), default="general")
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None),
                        nullable=False, index=True)


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


# ---------------------------------------------------------------------------
# Task-Schedule — agent long-task anti-stall (看板卡 t_6b29b140)
# ---------------------------------------------------------------------------
# Purpose: hermes/openclaw stall on long tasks — they do one step then yield to
# idle and wait. Claude Code doesn't, because its harness re-invokes the model
# after every tool result and it keeps a live TODO list. This pair of tables
# externalises that so a *fresh, zero-memory* woken session can resume a long
# task exactly where it left off:
#   - Task/TaskStep = the persistent TODO (survives compaction/session swap —
#     the thing an in-context TodoWrite list cannot),
#   - next_wake_at  = when an external cron should re-invoke the agent (the
#     substitute for the harness's automatic per-tool-result re-invocation),
#   - TaskStep.verify_cmd = how the woken session re-derives real progress from
#     the world (deterministic), since it has no memory of the prior run and
#     self-reported progress is untrustworthy.
# The runtime-specific glue (one recurring cron per agent that polls
# /tasks/due and spawns a session with the wake payload) lives outside HCC.


class TaskStatus(StrEnum):
    """Lifecycle of a whole :class:`Task`."""

    PENDING = "pending"      # registered, no step started yet
    RUNNING = "running"      # a step is in progress / being heartbeat-driven
    BLOCKED = "blocked"      # a step exceeded max attempts OR hit a redline — needs human
    DONE = "done"            # all steps verified complete
    FAILED = "failed"        # abandoned / unrecoverable
    CANCELLED = "cancelled"  # explicitly cancelled by owner


class StepStatus(StrEnum):
    """Lifecycle of a single :class:`TaskStep`."""

    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class Task(Base):
    """One long-running agent task, decomposed into ordered steps.

    ``current_step`` is the 0-based index into the task's steps that the
    heartbeat is currently driving. ``next_wake_at`` is when the external cron
    should next re-invoke the owning agent to push this task forward; a NULL
    means "not currently scheduled" (terminal, or awaiting first start).
    """

    __tablename__ = "tasks"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(128), index=True, nullable=False)
    agent_id = Column(String(64), index=True, default="default", nullable=False)
    title = Column(Text, nullable=False)
    goal = Column(Text, default="")           # the overall objective, injected into every wake
    status = Column(String(16), default=TaskStatus.PENDING.value, index=True)
    current_step = Column(Integer, default=0, nullable=False)
    # Extra redline keywords for THIS task, on top of the global redline list
    # (delete / spend money / external send / family domain). A step whose
    # instruction matches a redline blocks the task for human decision instead
    # of auto-advancing.
    redline_tags = Column(JSON, default=list)
    attempts_on_current = Column(Integer, default=0, nullable=False)  # denormalised for quick due-scan
    last_heartbeat = Column(DateTime, nullable=True)
    next_wake_at = Column(DateTime, nullable=True, index=True)        # cron scans WHERE next_wake_at <= now
    note = Column(Text, default="")           # last progress note / block reason
    # 循环任务(公子 09-03:「循环定时任务也是任务」)。非空则任务永不终态 DONE:
    # 全步验完后重置回第 0 步、status=running、next_wake_at 推到下次触发时刻。
    # 复用整套机制(租约/退避/红线/attempt 上限),不新起调度系统。格式见
    # task_service._next_fire:"every:<N>{s|m|h|d}" | "daily:HH:MM" | 纯秒数。
    repeat = Column(String(64), nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None), onupdate=lambda: datetime.now(timezone.utc).replace(tzinfo=None))

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','running','blocked','done','failed','cancelled')",
            name="ck_tasks_status",
        ),
    )


class TaskStep(Base):
    """One ordered step of a :class:`Task`.

    ``verify_cmd`` is the deterministic progress probe the woken agent runs in
    its OWN environment (the logs/artifacts may live on another machine than
    HCC) — e.g. ``tail -50 ~/train.log | grep -c 'epoch 100'``. ``est_seconds``
    drives the next wake interval; ``actual_seconds`` is filled on completion to
    feed future calibration. ``attempts`` counts heartbeats that found the step
    still unfinished.
    """

    __tablename__ = "task_steps"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    task_id = Column(String(36), index=True, nullable=False)
    idx = Column(Integer, nullable=False)         # 0-based order within the task
    title = Column(Text, nullable=False)
    instruction = Column(Text, default="")        # what the agent should DO this step
    verify_cmd = Column(Text, default="")         # deterministic "is it done?" probe (shell)
    est_seconds = Column(Integer, default=600, nullable=False)
    actual_seconds = Column(Integer, nullable=True)
    attempts = Column(Integer, default=0, nullable=False)
    status = Column(String(16), default=StepStatus.PENDING.value)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None), onupdate=lambda: datetime.now(timezone.utc).replace(tzinfo=None))

    __table_args__ = (
        UniqueConstraint("task_id", "idx", name="uq_task_steps_task_idx"),
        Index("ix_task_steps_task_id_idx", "task_id", "idx"),
    )


class PriorityStatus(StrEnum):
    """Lifecycle of a :class:`Priority` row (never physically deleted)."""

    ACTIVE = "active"           # 当前生效
    SUPERSEDED = "superseded"   # 被新版本取代(superseded_by 指向新行)
    EXPIRED = "expired"         # review_at 过期且已降级归档


class PriorityTrust(StrEnum):
    """信任级别 —— 门槛 B(隔离生效):pending 先半权重,公子确认后转正全权重。"""

    CONFIRMED = "confirmed"     # 公子显式确认 → 全权重
    PENDING = "pending"         # agent 提案 → 确认前半权重(压不动紧急、也不污染全局)


class Priority(Base):
    """公子的『价值坐标』:一条"现在什么重要/急"的一等公民条目(跨运行时共享)。

    设计见 docs/priority-compass-design.md。核心铁律:**价值读时算,绝不落进
    memories.importance**。每条记忆的有效重要性 = 记忆 × 本表 的 join,在
    context_builder 读路现算;改一行本表 → 全库权重瞬间刷新,无回刷、无重判。

    象限(不落列,读时派生):imp≥4∧urg≥4→Q1;imp≥4→Q2;urg≥4→Q3;else Q4。
    """

    __tablename__ = "priorities"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(128), index=True, nullable=False, default="michael")
    label = Column(Text, nullable=False)                       # 「肩颈损伤恢复」
    anchors = Column(JSON, default=list)                       # 主题锚词(加速 join,读路用)
    importance = Column(Integer, default=3, nullable=False)    # 1-5
    urgency = Column(Integer, default=3, nullable=False)       # 1-5
    source = Column(String(64), default="gongzi")              # gongzi | agent:<name>
    trust = Column(String(16), default=PriorityTrust.CONFIRMED.value)
    status = Column(String(16), default=PriorityStatus.ACTIVE.value, index=True)
    review_at = Column(Date, nullable=True)                    # 复核日:过期 7 天未复核 → α 减半
    superseded_by = Column(String(36), nullable=True)          # 版本链,永不物删
    embedding = Column(Vector(EMBEDDING_DIM), nullable=True)   # label 向量(预留 emb join)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None), onupdate=lambda: datetime.now(timezone.utc).replace(tzinfo=None))

    __table_args__ = (
        CheckConstraint("importance BETWEEN 1 AND 5", name="ck_priorities_importance"),
        CheckConstraint("urgency BETWEEN 1 AND 5", name="ck_priorities_urgency"),
        CheckConstraint("status IN ('active','superseded','expired')", name="ck_priorities_status"),
        CheckConstraint("trust IN ('confirmed','pending')", name="ck_priorities_trust"),
    )

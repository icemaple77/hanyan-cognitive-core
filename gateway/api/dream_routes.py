"""Dream API routes — HCC-native three-phase memory consolidation (v2).

Additive to the legacy ``POST /api/v1/dream/consolidate`` in
``cognitive_routes.py`` (left unchanged for backward compat). This is the new
P0 surface from ``docs/dreaming-design.md``: manual triggers for each phase
plus a status endpoint, mirroring the three background loops started in
``gateway/main.py``'s lifespan.

Manual triggers here go through the *same* idempotency guard the background
loops use (``DreamEngine._latest_run_today``), so calling e.g.
``POST /dream/deep`` twice in one day is safe by default — pass
``{"force": true}`` to intentionally re-run for testing.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import String, cast, select

from core.dream import DreamEngine, get_dream_engine
from core.forget import PROTECTED_TAGS
from gateway.core.database import async_session
from gateway.core.embeddings import EMBEDDING_MODEL, embed_text, memory_embedding_text
from gateway.models import DreamRun, Memory, MemoryStatus

logger = logging.getLogger(__name__)

router = APIRouter()


class DreamTriggerRequest(BaseModel):
    force: bool = False


@router.post("/dream/light", summary="Trigger Light-phase consolidation (idempotent per day per memory)")
async def trigger_light(data: DreamTriggerRequest | None = None) -> dict:
    data = data or DreamTriggerRequest()
    try:
        return await get_dream_engine().run_light(force=data.force)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/dream/rem", summary="Trigger REM-phase consolidation (idempotent per day)")
async def trigger_rem(data: DreamTriggerRequest | None = None) -> dict:
    data = data or DreamTriggerRequest()
    try:
        return await get_dream_engine().run_rem(force=data.force)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/dream/deep", summary="Trigger Deep-phase consolidation (writes Memory + diary, idempotent per day)")
async def trigger_deep(data: DreamTriggerRequest | None = None) -> dict:
    data = data or DreamTriggerRequest()
    try:
        return await get_dream_engine().run_deep(force=data.force)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/dream/status", summary="Last run per phase + configured thresholds")
async def dream_status() -> dict:
    return await get_dream_engine().status()


# --- 摘要阶段(digest):做梦的第四步,由 scripts/daily_digest.py 每天早上驱动 ---------
#
# Deep 只挑出"哪几组记忆值得巩固",真正把一组记忆写成知识的是 umbrella 上的大模型。
# 大模型不在网关进程里跑(要开机、要占显存、一次几分钟),所以网关只提供三个口:
# 领活(pending-knowledge)、交活(knowledge)、记一笔(runs)。

LLM_SUMMARY_TAG = "llm-summary"
_MEMBER_CHARS = 400
_MAX_MEMBERS = 12


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


@router.get("/dream/pending-knowledge", summary="等大模型来写的知识组(Deep 新挑出的 + 存量模板空壳)")
async def pending_knowledge(limit: int = 30, days: int = 7) -> dict:
    groups: list[dict] = []
    async with async_session() as session:
        known = (await session.execute(
            select(Memory).where(Memory.type == "knowledge", Memory.source == "dream",
                                 Memory.status == MemoryStatus.ACTIVE)
        )).scalars().all()
        by_cluster: dict[str, Memory] = {}
        for km in known:
            for t in km.tags or []:
                if isinstance(t, str) and t.startswith("dream-cluster:"):
                    by_cluster[t.split(":", 1)[1]] = km

        wanted: list[tuple[str, str | None, list[str]]] = []  # (key, existing_id, member_ids)
        seen: set[str] = set()
        runs = (await session.execute(
            select(DreamRun).where(DreamRun.phase == "deep",
                                   DreamRun.started_at >= _utcnow() - timedelta(days=max(1, days)))
            .order_by(DreamRun.started_at.desc())
        )).scalars().all()
        for run in runs:
            for g in (run.stats or {}).get("knowledge_groups") or []:
                key = g.get("key")
                if not key or key in seen:
                    continue
                seen.add(key)
                existing = by_cluster.get(key)
                if existing is not None and LLM_SUMMARY_TAG in (existing.tags or []):
                    continue  # 已经有大模型写的版本了
                wanted.append((key, existing.id if existing is not None else None, list(g.get("member_ids") or [])))
        # 存量:模板拼出来的空壳(没有 llm-summary 标签的 dream 知识)
        for km in known:
            tags = km.tags or []
            if LLM_SUMMARY_TAG in tags or set(tags) & PROTECTED_TAGS:
                continue
            key = next((t.split(":", 1)[1] for t in tags if isinstance(t, str) and t.startswith("dream-cluster:")), f"legacy-{km.id}")
            if key in seen:
                continue
            seen.add(key)
            wanted.append((key, km.id, DreamEngine._parse_source_ids(km.content or "")))

        total = len(wanted)
        for key, existing_id, member_ids in wanted[: max(1, limit)]:
            members = []
            if member_ids:
                rows = (await session.execute(
                    select(Memory).where(Memory.id.in_(member_ids[:_MAX_MEMBERS * 3]))
                    .order_by(Memory.created_at)
                )).scalars().all()
                for m in rows[:_MAX_MEMBERS]:
                    text_ = (m.content or "").strip().replace("\n", " ")[:_MEMBER_CHARS]
                    if text_:
                        members.append({"id": m.id, "text": text_})
            groups.append({"key": key, "existing_id": existing_id, "members": members})
    return {"groups": groups, "total_pending": total}


class KnowledgeUpsert(BaseModel):
    key: str
    existing_id: str | None = None
    title: str = ""
    content: str = ""  # 留空 = 这组提炼不出知识:有空壳就软删,没有就什么都不做
    member_ids: list[str] = Field(default_factory=list)


@router.post("/dream/knowledge", summary="交回一条由大模型写成的知识(按簇 key 幂等)")
async def upsert_knowledge(data: KnowledgeUpsert) -> dict:
    cluster_tag = f"dream-cluster:{data.key}"
    async with async_session() as session:
        existing = await session.get(Memory, data.existing_id) if data.existing_id else None
        if existing is None:
            existing = (await session.execute(
                select(Memory).where(Memory.type == "knowledge", Memory.source == "dream",
                                     Memory.status == MemoryStatus.ACTIVE,
                                     cast(Memory.tags, String).like(f'%"{cluster_tag}"%'))
            )).scalars().first()
        content = data.content.strip()
        if not content:
            if existing is not None and not (set(existing.tags or []) & PROTECTED_TAGS):
                existing.status = MemoryStatus.DISCARDED
                existing.tags = [*(existing.tags or []), "empty-shell"]
                existing.updated_at = _utcnow()
                await session.commit()
                return {"action": "discarded", "id": existing.id}
            return {"action": "skipped", "id": None}

        title = data.title.strip()[:120] or content[:60]
        body = content
        if data.member_ids:
            body += f"\n\n来源记忆 ({len(data.member_ids)}): {', '.join(sorted(data.member_ids))}"
        try:
            embedding = await asyncio.to_thread(embed_text, memory_embedding_text(body, title))
        except Exception:
            logger.exception("dream knowledge: embed failed — storing without embedding")
            embedding = None
        if existing is not None:
            existing.content, existing.summary = body, title
            keep = [t for t in (existing.tags or []) if t != LLM_SUMMARY_TAG]
            existing.tags = list(dict.fromkeys([*keep, cluster_tag, "auto-knowledge", LLM_SUMMARY_TAG]))
            existing.embedding = embedding
            existing.embedding_model = EMBEDDING_MODEL if embedding is not None else None
            existing.updated_at = _utcnow()
            await session.commit()
            return {"action": "updated", "id": existing.id}
        mem = Memory(
            user_id="system", agent_id="openclaw", type="knowledge", source="dream",
            content=body, summary=title, importance=0.7,
            tags=[cluster_tag, "auto-knowledge", LLM_SUMMARY_TAG],
            embedding=embedding, embedding_model=EMBEDDING_MODEL if embedding is not None else None,
        )
        session.add(mem)
        await session.commit()
        return {"action": "created", "id": mem.id}


class DreamRunReport(BaseModel):
    phase: str = "digest"
    started_at: datetime | None = None
    stats: dict = Field(default_factory=dict)


@router.post("/dream/runs", summary="外部阶段(摘要)跑完后记一笔,/dream/status 里看得到")
async def record_run(data: DreamRunReport) -> dict:
    if data.phase != "digest":
        raise HTTPException(status_code=422, detail="only phase=digest is reported from outside")
    started = data.started_at.astimezone(timezone.utc).replace(tzinfo=None) if data.started_at and data.started_at.tzinfo else (data.started_at or _utcnow())
    async with async_session() as session:
        run = DreamRun(phase="digest", started_at=started, finished_at=_utcnow(), stats=data.stats)
        session.add(run)
        await session.commit()
        return {"id": run.id}

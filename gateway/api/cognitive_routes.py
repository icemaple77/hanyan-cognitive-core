"""Orchestrator + Forget + Personality API routes."""

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.core.database import get_session
from gateway.models import Memory
from gateway.schemas.memory import MemorySearch
from gateway.services import MemoryService
from core.orchestrator import get_orchestrator
from core.forget import get_forget_engine
from core.personality import get_personality_engine
from core.subconscious import get_subconscious
from core.model_router import get_model_router
from core.optimizer import get_optimizer
from core.knowledge_indexer import get_indexer
from core.dream import get_dream_engine
from pydantic import BaseModel

router = APIRouter()


class EvaluateRequest(BaseModel):
    content: str
    source: str = "conversation"
    user_id: str = "default"
    agent_id: str | None = None


@router.post("/orchestrator/evaluate", summary="Evaluate whether to store")
async def evaluate_content(data: EvaluateRequest):
    orchestrator = get_orchestrator()
    decision = orchestrator.evaluate(data.content, data.source, data.user_id)
    return {
        "should_store": decision.should_store,
        "importance": decision.importance,
        "reason": decision.reason,
        "suggested_tags": decision.suggested_tags,
    }


def _scan_memories(memories: list[Memory]) -> list[dict]:
    """跑遗忘引擎的真实衰减公式(用真实access_count/last_access,不再是硬编码0/None)。"""
    engine = get_forget_engine()
    out = []
    for m in memories:
        decision = engine.process({
            "id": m.id,
            "content": m.content,
            "importance": m.importance,
            "access_count": m.access_count,
            "last_access": m.last_access,
            "created_at": m.created_at,
            "status": m.status,
        })
        decision["preview"] = m.content[:60]
        out.append(decision)
    return out


@router.get("/forget/scan", summary="Scan memories for decay(只读,不写库)")
async def scan_for_forget(agent_id: str | None = None, limit: int = 200,
                          session: AsyncSession = Depends(get_session)):
    stmt = select(Memory).where(Memory.status == "active")
    if agent_id:
        stmt = stmt.where(Memory.agent_id == agent_id)
    stmt = stmt.order_by(Memory.created_at.desc()).limit(limit)
    result = await session.execute(stmt)
    memories = list(result.scalars().all())
    results = _scan_memories(memories)
    return {"scanned": len(results), "results": results}


class ForgetApplyRequest(BaseModel):
    agent_id: str | None = None
    limit: int = 500
    dry_run: bool = True


@router.post("/forget/apply", summary="真正执行遗忘(归档,永不物理删除)")
async def apply_forget(data: ForgetApplyRequest, session: AsyncSession = Depends(get_session)):
    """scan 只建议,这个才真的写库。archive/delete 建议统一映射成"归档"
    (status=archived,移出默认检索范围)——按公子原设计,记忆不物理删除,
    真删除是以后接 NAS 冷备份才该做的事,现在宁可保守。"""
    stmt = select(Memory).where(Memory.status == "active")
    if data.agent_id:
        stmt = stmt.where(Memory.agent_id == data.agent_id)
    stmt = stmt.order_by(Memory.created_at.desc()).limit(data.limit)
    result = await session.execute(stmt)
    memories = list(result.scalars().all())
    decisions = _scan_memories(memories)

    to_archive = [d for d in decisions if d["suggested_action"] in ("archive", "delete")]
    if not data.dry_run:
        by_id = {m.id: m for m in memories}
        for d in to_archive:
            by_id[d["id"]].status = "archived"
        await session.commit()

    return {
        "scanned": len(decisions),
        "archived": len(to_archive) if not data.dry_run else 0,
        "would_archive": len(to_archive),
        "dry_run": data.dry_run,
        "archived_ids": [d["id"] for d in to_archive] if not data.dry_run else [],
    }


@router.get("/personality/summary", summary="Get personality profile")
async def get_personality_summary():
    engine = get_personality_engine()
    return engine.get_summary()


@router.post("/personality/process", summary="Process text for personality")
async def process_personality(data: EvaluateRequest):
    engine = get_personality_engine()
    updated = engine.process_text(data.content, data.source)
    return {"updated_preferences": updated, "summary": engine.get_summary()}


class _DBMemoryProvider:
    """把 MemoryService 包成 subconscious.retrieve() 期望的 memory_provider 接口。
    之前 subconscious_retrieve 路由从没传 memory_provider,导致三层检索的
    preconscious/subconscious 两层(真正查数据库的部分)从未被执行——只有
    conscious 层(当次请求内存)在跑,575 条历史记忆实际上从未被真正检索过。"""
    def __init__(self, session: AsyncSession):
        self.service = MemoryService(session)

    async def search(self, query: str, user_id: str | None = None,
                     agent_id: str | None = None, limit: int = 10) -> dict:
        q = MemorySearch(query=query, user_id=user_id, agent_id=agent_id, limit=limit)
        memories, _total = await self.service.search(q)
        return {"items": [
            {"id": m.id, "content": m.content, "importance": m.importance, "tags": m.tags or []}
            for m in memories
        ]}


@router.post("/subconscious/retrieve", summary="Three-layer memory retrieval")
async def subconscious_retrieve(data: EvaluateRequest, session: AsyncSession = Depends(get_session)):
    sub = get_subconscious()
    sub.add_to_conscious(data.content, data.source)
    provider = _DBMemoryProvider(session)
    results = await sub.retrieve(data.content, memory_provider=provider, limit=10,
                                 user_id=data.user_id, agent_id=data.agent_id)
    return {
        "conscious_count": len(sub.get_conscious()),
        "results": [
            {"content": r.content[:100], "source": r.source, "score": r.score,
             "importance": r.importance, "memory_id": r.memory_id}
            for r in results[:5]
        ],
    }


@router.get("/subconscious/conscious", summary="Get current conscious context")
async def get_conscious():
    sub = get_subconscious()
    return {"context": sub.get_conscious_context(max_chars=1000)}


@router.get("/router/summary", summary="Get model router config")
async def router_summary():
    router = get_model_router()
    return router.get_profile_summary()


@router.post("/router/profile", summary="Switch hardware profile")
async def set_profile(profile: str):
    router = get_model_router()
    router.set_profile(profile)
    return {"profile": profile, "summary": router.get_profile_summary()}


class OptimizeRequest(BaseModel):
    workspace_dir: str
    agent_id: str = "default"
    dry_run: bool = False


@router.post("/optimizer/scan", summary="Scan workspace for absorbable files")
async def scan_workspace(data: OptimizeRequest):
    opt = get_optimizer(data.workspace_dir)
    scan = opt.scan_all_files()
    return {
        "workspace": data.workspace_dir,
        "agent_id": data.agent_id,
        "bootstrap": [str(p) for p in scan["bootstrap"]],
        "absorbable": [str(p) for p in scan["absorbable"]],
        "other": [str(p) for p in scan["other"]],
    }


@router.post("/optimizer/run", summary="Run full optimization cycle")
async def run_optimization(data: OptimizeRequest):
    opt = get_optimizer(data.workspace_dir)
    result = opt.optimize(dry_run=data.dry_run, memory_count=42)
    result["agent_id"] = data.agent_id
    return result


@router.post("/optimizer/bootstrap", summary="Generate bootstrap files")
async def generate_bootstrap(data: OptimizeRequest):
    opt = get_optimizer(data.workspace_dir)
    contents = opt.generate_bootstrap(memory_count=42)
    written = opt.write_bootstrap(contents)
    return {
        "workspace": data.workspace_dir,
        "agent_id": data.agent_id,
        "files_written": [str(p) for p in written]
    }


class IndexRequest(BaseModel):
    workspace_dir: str
    api_base_url: str = "http://localhost:8000"
    agent_id: str = "main"
    dry_run: bool = False


@router.post("/indexer/scan", summary="Scan workspace knowledge files")
async def scan_knowledge(data: IndexRequest):
    idx = get_indexer(data.workspace_dir)
    scan = idx.scan()
    return {
        "workspace": data.workspace_dir,
        "found": sum(len(v) for v in scan.values()),
        "by_category": {k: len(v) for k, v in scan.items()},
    }


@router.post("/indexer/run", summary="Index workspace knowledge into HCC")
async def run_indexer(data: IndexRequest):
    idx = get_indexer(data.workspace_dir)
    scan = idx.scan()
    if not scan:
        return {"status": "nothing_to_index", "found": 0}
    if data.dry_run:
        return {
            "status": "dry_run",
            "found": sum(len(v) for v in scan.values()),
            "by_category": {k: len(v) for k, v in scan.items()},
        }
    result = idx.index_to_knowledge(scan, data.api_base_url, data.agent_id)
    return result


class DreamRequest(BaseModel):
    agent_id: str = "default"
    limit: int = 200
    persist_knowledge: bool = True


@router.post("/dream/consolidate", summary="Nightly memory consolidation (dream)")
async def dream_consolidate(data: DreamRequest, session: AsyncSession = Depends(get_session)):
    """做梦:聚类相似记忆 → 合并近似重复 → 提炼模式 → 生成知识摘要。
    knowledge 条目会作为新的 memory(type=knowledge)存回,供白天检索命中。
    只读取原始记忆生成摘要,不删除/修改原记忆(遗忘交给 forget_engine 单独处理)。
    """
    engine = get_dream_engine()
    q = select(Memory).where(Memory.agent_id == data.agent_id, Memory.status == "active")
    q = q.order_by(Memory.created_at.desc()).limit(data.limit)
    result = await session.execute(q)
    memories = [
        {"id": m.id, "content": m.content, "summary": m.summary,
         "importance": m.importance, "tags": m.tags or [],
         "created_at": m.created_at, "updated_at": m.updated_at}
        for m in result.scalars().all()
    ]

    outcome = await engine.consolidate(memories)

    stored_ids = []
    if data.persist_knowledge and outcome["knowledge"]:
        created = []
        for k in outcome["knowledge"]:
            mem = Memory(
                user_id="system", agent_id=data.agent_id, type="knowledge",
                content=k["summary"], summary=k["title"],
                importance=k["importance"], tags=k["tags"], source="dream",
            )
            session.add(mem)
            created.append(mem)
        # id 是 Column(default=uuid4) 的客户端默认值,flush 时才真正生成/可读,
        # 之前在 add() 后立刻读 mem.id 一直是 None(数据本身存对了,只是回传的id不对)
        await session.flush()
        stored_ids = [m.id for m in created]
        await session.commit()

    outcome["stored_knowledge_ids"] = stored_ids
    return outcome

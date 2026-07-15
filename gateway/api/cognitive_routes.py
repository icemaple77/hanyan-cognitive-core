"""Orchestrator + Forget + Personality API routes."""

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.core.database import get_session
from gateway.models import Memory
from core.orchestrator import get_orchestrator
from core.forget import get_forget_engine
from core.personality import get_personality_engine
from core.subconscious import get_subconscious
from core.model_router import get_model_router
from core.optimizer import get_optimizer
from pydantic import BaseModel

router = APIRouter()


class EvaluateRequest(BaseModel):
    content: str
    source: str = "conversation"
    user_id: str = "default"


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


@router.get("/forget/scan", summary="Scan memories for decay")
async def scan_for_forget(session: AsyncSession = Depends(get_session)):
    engine = get_forget_engine()
    result = await session.execute(select(Memory).order_by(Memory.created_at.desc()).limit(100))
    memories = list(result.scalars().all())

    results = []
    for m in memories:
        stats = engine.get_stats({
            "id": m.id,
            "content": m.content,
            "importance": m.importance,
            "access_count": 0,
            "last_access": None,
            "created_at": m.created_at,
            "status": m.status,
        })
        results.append({
            "id": stats.id,
            "preview": stats.content[:60],
            "importance": stats.importance,
            "forget_score": stats.forget_score,
            "days_since_access": stats.days_since_access,
            "suggested_action": "archive" if stats.forget_score > 0.6 else "keep",
        })

    return {"scanned": len(results), "results": results}


@router.get("/personality/summary", summary="Get personality profile")
async def get_personality_summary():
    engine = get_personality_engine()
    return engine.get_summary()


@router.post("/personality/process", summary="Process text for personality")
async def process_personality(data: EvaluateRequest):
    engine = get_personality_engine()
    updated = engine.process_text(data.content, data.source)
    return {"updated_preferences": updated, "summary": engine.get_summary()}


@router.post("/subconscious/retrieve", summary="Three-layer memory retrieval")
async def subconscious_retrieve(data: EvaluateRequest):
    sub = get_subconscious()
    sub.add_to_conscious(data.content, data.source)
    results = await sub.retrieve(data.content, limit=10, user_id=data.user_id)
    return {
        "conscious_count": len(sub.get_conscious()),
        "results": [
            {"content": r.content[:100], "source": r.source, "score": r.score, "importance": r.importance}
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

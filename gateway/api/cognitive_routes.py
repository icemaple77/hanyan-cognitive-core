"""Orchestrator + Forget + Personality API routes."""

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.core.database import get_session
from gateway.models import Memory
from core.orchestrator import get_orchestrator
from core.forget import get_forget_engine
from core.personality import get_personality_engine
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
            "access_count": getattr(m, "access_count", 0),
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

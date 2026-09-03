"""Manual trigger for the per-agent_id memory export (core/agent_export.py).

No automatic background loop is wired for this one — it's cheap enough (a
full-table scan + markdown writes) to run from cron alongside
scripts/backup_hcc.sh / scripts/index_documents.py rather than adding a
fourth in-process asyncio loop. This endpoint exists for on-demand /
post-write triggering (e.g. from an agent that just stored something and
wants its own export refreshed immediately).
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from core.agent_export import AgentExporter

router = APIRouter()


@router.post("/export/agents", summary="Regenerate the per-agent_id markdown export")
async def trigger_agent_export() -> dict:
    try:
        stats = await AgentExporter().generate_all()
        return stats.as_dict()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

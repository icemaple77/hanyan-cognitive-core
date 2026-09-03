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

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from core.dream import get_dream_engine

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

"""Emotion API routes (docs/emotion-design.md).

Two response shapes off the same engine:

* **Full mode** (``GET /emotion/state``) — the 6-dim state + named state +
  expression hint, for agent/prompt consumption.
* **Display mode** (``GET /emotion/display``) — ``{emotion, emotion_en,
  intensity, color, icon, updated_at}``, for the aicore USB small-screen (or
  any other lightweight client) that has no business knowing HCC's internal
  dimension model.
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.emotion import get_emotion_engine
from gateway.core.database import get_session
from gateway.models import EmotionSnapshot

router = APIRouter()


class EmotionUpdateRequest(BaseModel):
    text: str
    source: str = "conversation"
    importance: float | None = None


@router.get("/emotion/state", summary="Get current emotional state (full mode, for agent use)")
async def get_emotion_state():
    engine = get_emotion_engine()
    return engine.get_summary()


@router.get("/emotion/display", summary="Get current emotional state (display mode, for the small screen)")
async def get_emotion_display():
    engine = get_emotion_engine()
    return engine.get_display_summary()


@router.post("/emotion/update", summary="Update emotion from text")
async def update_emotion(data: EmotionUpdateRequest):
    engine = get_emotion_engine()
    state = await engine.update_and_persist(data.text, data.source, importance=data.importance)
    return {"state": state, "named_state": engine.get_summary()["named_state"]}


@router.get("/emotion/history", summary="Get emotion history (recent in-memory trigger log)")
async def get_emotion_history():
    engine = get_emotion_engine()
    return engine.to_dict()


@router.get("/emotion/snapshots", summary="Get daily cold snapshots (Deep-phase, for mood trend review)")
async def get_emotion_snapshots(
    days: int = Query(default=30, ge=1, le=365),
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(
        select(EmotionSnapshot).order_by(EmotionSnapshot.snapshot_date.desc()).limit(days)
    )
    rows = result.scalars().all()
    return {
        "snapshots": [
            {
                "date": row.snapshot_date.isoformat() if isinstance(row.snapshot_date, date) else row.snapshot_date,
                "state": row.state,
                "named_state": row.named_state,
                "dominant_trigger": row.dominant_trigger,
            }
            for row in rows
        ]
    }

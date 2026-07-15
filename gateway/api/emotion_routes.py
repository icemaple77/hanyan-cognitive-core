"""Emotion API routes."""

from fastapi import APIRouter
from pydantic import BaseModel

from core.emotion import get_emotion_engine

router = APIRouter()


class EmotionUpdateRequest(BaseModel):
    text: str
    source: str = "conversation"


@router.get("/emotion/state", summary="Get current emotional state")
async def get_emotion_state():
    engine = get_emotion_engine()
    return engine.get_summary()


@router.post("/emotion/update", summary="Update emotion from text")
async def update_emotion(data: EmotionUpdateRequest):
    engine = get_emotion_engine()
    state = engine.update(data.text, data.source)
    return {"state": state}


@router.get("/emotion/history", summary="Get emotion history")
async def get_emotion_history():
    engine = get_emotion_engine()
    return engine.to_dict()

"""Emotion Engine — internal state for the AI companion.

Maintains dimensional emotional state that updates based on conversation.
Dimensions: happiness, curiosity, fatigue, worry, closeness, focus.
"""

from __future__ import annotations

import json
import math
import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# Emotional dimensions with default neutral values
DEFAULT_STATE: dict[str, float] = {
    "happiness": 0.6,
    "curiosity": 0.7,
    "fatigue": 0.2,
    "worry": 0.1,
    "closeness": 0.5,
    "focus": 0.6,
}

# Keywords that trigger emotional shifts
EMOTION_TRIGGERS: dict[str, dict[str, float]] = {
    "success": {"happiness": 0.15, "curiosity": -0.05, "fatigue": -0.05},
    "failed": {"happiness": -0.15, "worry": 0.15, "fatigue": 0.1},
    "happy": {"happiness": 0.2, "worry": -0.1},
    "sad": {"happiness": -0.2, "worry": 0.1, "fatigue": 0.1},
    "tired": {"fatigue": 0.25, "focus": -0.1},
    "love": {"closeness": 0.2, "happiness": 0.1},
    "miss": {"closeness": 0.15, "happiness": -0.05},
    "thank": {"closeness": 0.1, "happiness": 0.05},
    "angry": {"happiness": -0.2, "worry": 0.1, "fatigue": 0.05},
    "excited": {"happiness": 0.2, "curiosity": 0.1, "fatigue": -0.1},
    "bored": {"curiosity": -0.15, "focus": -0.1},
    "curious": {"curiosity": 0.2, "focus": 0.1},
    "proud": {"happiness": 0.15, "closeness": 0.05},
    "worried": {"worry": 0.2, "happiness": -0.1},
    "relaxed": {"fatigue": -0.15, "happiness": 0.05},
}


class EmotionEngine:
    """Simple dimensional emotion engine.

    State decays toward neutral over time. Emotion triggers from keywords
    in conversation cause shifts in dimensions.
    """

    def __init__(self):
        self._state: dict[str, float] = dict(DEFAULT_STATE)
        self._last_update: datetime = datetime.now(timezone.utc)
        self._history: list[dict[str, Any]] = []

    @property
    def state(self) -> dict[str, float]:
        """Current emotional state (with time decay applied)."""
        self._apply_decay()
        return dict(self._state)

    def _apply_decay(self) -> None:
        """Gradually decay emotions toward neutral."""
        now = datetime.now(timezone.utc)
        hours = (now - self._last_update).total_seconds() / 3600
        if hours < 0.01:
            return

        decay_rate = 0.05 * hours  # 5% per hour toward neutral
        for dim in self._state:
            neutral = DEFAULT_STATE.get(dim, 0.5)
            self._state[dim] += (neutral - self._state[dim]) * min(decay_rate, 0.5)
        self._last_update = now

    def update(self, text: str, source: str = "conversation") -> dict[str, float]:
        """Update emotional state based on text content."""
        self._apply_decay()
        text_lower = text.lower()

        # Apply triggers
        for keyword, shifts in EMOTION_TRIGGERS.items():
            if keyword in text_lower:
                for dim, shift in shifts.items():
                    if dim in self._state:
                        self._state[dim] = max(0.0, min(1.0, self._state[dim] + shift))

        # Record snapshot
        snapshot = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "source": source,
            "state": dict(self._state),
            "triggered_by": [k for k in EMOTION_TRIGGERS if k in text_lower],
        }
        self._history.append(snapshot)
        if len(self._history) > 1000:
            self._history = self._history[-500:]

        return dict(self._state)

    def get_summary(self) -> dict[str, Any]:
        """Get emotional summary for prompt injection."""
        s = self.state
        primary = max(s, key=s.get) if s else "neutral"
        return {
            "state": s,
            "primary_emotion": primary,
            "intensity": f"{s.get(primary, 0):.2f}",
            "last_update": self._last_update.isoformat(),
        }

    def to_dict(self) -> dict[str, Any]:
        """Full state dump including history."""
        return {
            "state": self.state,
            "history_count": len(self._history),
            "recent_history": self._history[-10:] if self._history else [],
            "last_update": self._last_update.isoformat(),
        }


# Singleton for use across the app
_emotion_engine: EmotionEngine | None = None


def get_emotion_engine() -> EmotionEngine:
    global _emotion_engine
    if _emotion_engine is None:
        _emotion_engine = EmotionEngine()
    return _emotion_engine

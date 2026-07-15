"""Forget Engine — memory lifecycle management.

Implements importance decay, automatic archiving, and eventual deletion.
Memories naturally fade over time unless reinforced by repeated access.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# Default thresholds
DEFAULT_IMPORTANCE_DECAY = 0.05  # 5% decay per period
DECAY_PERIOD_HOURS = 24  # one decay unit = 1 day
ARCHIVE_THRESHOLD = 0.15  # below this → archive
DELETE_THRESHOLD = 0.05  # below this → delete
PROMOTION_BOOST = 0.1  # each access boosts importance


@dataclass
class MemoryStats:
    """Read-only stats for a memory's lifecycle state."""
    id: str
    content: str
    importance: float
    access_count: int
    days_since_created: float
    days_since_access: float
    forget_score: float  # 0.0 = fresh, 1.0 = forgotten
    status: str  # active / archived / deleted
    created_at: str
    last_access: str | None


class ForgetEngine:
    """Manages memory lifecycle: decay, archive, delete.

    Each memory has:
    - importance: how valuable it is (0-1)
    - access_count: how often it's been retrieved
    - days_since_access: recency factor
    - forget_score: composite score (higher = more forgotten)
    """

    def __init__(self, decay_rate: float = DEFAULT_IMPORTANCE_DECAY):
        self.decay_rate = decay_rate

    def calculate_forget_score(self, importance: float, access_count: int,
                                days_since_access: float) -> float:
        """Calculate how forgotten a memory is (0.0 = fresh, 1.0 = forgotten).

        Formula: base_decay × time_factor / (reinforcement + 1)
        - base_decay: starts from (1 - importance)
        - time_factor: logarithmic time since last access
        - reinforcement: each access count reinforces
        """
        base_decay = 1.0 - importance
        time_factor = math.log2(max(1.0, days_since_access + 1)) / 30.0  # normalize
        reinforcement = math.log2(max(1, access_count + 1)) / 5.0
        score = base_decay * (1.0 + time_factor) / (1.0 + reinforcement)
        return min(1.0, max(0.0, score))

    def process(self, memory: dict[str, Any]) -> dict[str, Any]:
        """Process a single memory: compute forget score and suggest action."""
        now = datetime.now(timezone.utc)

        created = memory.get("created_at")
        if isinstance(created, str):
            created_dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
        else:
            created_dt = created or now

        last_access = memory.get("last_access")
        if last_access:
            if isinstance(last_access, str):
                access_dt = datetime.fromisoformat(last_access.replace("Z", "+00:00"))
            else:
                access_dt = last_access
        else:
            access_dt = created_dt

        days_since_created = max(0.0, (now - created_dt).total_seconds() / 86400)
        days_since_access = max(0.0, (now - access_dt).total_seconds() / 86400)

        importance = memory.get("importance", 0.5)
        access_count = memory.get("access_count", 0)

        forget_score = self.calculate_forget_score(importance, access_count, days_since_access)

        # Apply decay to importance
        decayed_importance = importance * (1.0 - self.decay_rate) ** (days_since_access / 7.0)

        # Determine action
        current_status = memory.get("status", "active")
        if decayed_importance <= DELETE_THRESHOLD and days_since_access > 90:
            suggested_action = "delete"
        elif decayed_importance <= ARCHIVE_THRESHOLD or current_status == "archived":
            suggested_action = "archive"
        else:
            suggested_action = "keep"

        return {
            "id": memory.get("id"),
            "forget_score": round(forget_score, 4),
            "decayed_importance": round(decayed_importance, 4),
            "days_since_access": round(days_since_access, 1),
            "access_count": access_count,
            "suggested_action": suggested_action,
        }

    def on_access(self, memory: dict[str, Any]) -> float:
        """Called when a memory is accessed — boost importance."""
        importance = memory.get("importance", 0.5)
        return min(1.0, importance + PROMOTION_BOOST)

    def batch_process(self, memories: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Process multiple memories and return actions."""
        return [self.process(m) for m in memories]

    def get_stats(self, memory: dict[str, Any]) -> MemoryStats:
        """Get detailed stats for display/debug."""
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        created = memory.get("created_at")
        last_access = memory.get("last_access")

        if isinstance(created, str):
            created_dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
        else:
            created_dt = created or now
        if hasattr(created_dt, 'tzinfo') and created_dt.tzinfo is not None:
            created_dt = created_dt.astimezone(timezone.utc).replace(tzinfo=None)

        if last_access:
            if isinstance(last_access, str):
                access_dt = datetime.fromisoformat(last_access.replace("Z", "+00:00"))
            else:
                access_dt = last_access
            if hasattr(access_dt, 'tzinfo') and access_dt.tzinfo is not None:
                access_dt = access_dt.astimezone(timezone.utc).replace(tzinfo=None)
        else:
            access_dt = created_dt

        importance = memory.get("importance", 0.5)
        access_count = memory.get("access_count", 0)
        days_since_created = max(0.0, (now - created_dt).total_seconds() / 86400)
        days_since_access = max(0.0, (now - access_dt).total_seconds() / 86400)
        forget_score = self.calculate_forget_score(importance, access_count, days_since_access)

        return MemoryStats(
            id=memory.get("id", ""),
            content=(memory.get("content") or "")[:80],
            importance=importance,
            access_count=access_count,
            days_since_created=round(days_since_created, 1),
            days_since_access=round(days_since_access, 1),
            forget_score=round(forget_score, 4),
            status=memory.get("status", "active"),
            created_at=str(created_dt),
            last_access=str(access_dt) if last_access else None,
        )


# Singleton
_forget_engine: ForgetEngine | None = None


def get_forget_engine() -> ForgetEngine:
    global _forget_engine
    if _forget_engine is None:
        _forget_engine = ForgetEngine()
    return _forget_engine

"""Personality Engine — tracks user preferences and personality traits.

Preferences start neutral and strengthen with repeated exposure.
The engine learns: what the user likes, how they prefer to work,
their communication style, and recurring topics.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# Initial preference score for newly discovered preferences
INITIAL_SCORE = 0.3
BOOST_PER_MENTION = 0.08  # Each mention boosts by this much
DECAY_PER_DAY = 0.01  # Slow decay if not mentioned
ARCHIVE_SCORE = 0.1  # Below this, preference is archived
MAX_SCORE = 0.99


@dataclass
class Preference:
    """A learned preference or trait."""
    name: str
    category: str  # interest, work_style, communication, topic, value
    score: float  # 0.0-1.0 confidence
    mention_count: int
    first_seen: str
    last_seen: str
    examples: list[str] = field(default_factory=list)


# Known preference patterns
PREFERENCE_PATTERNS: dict[str, dict[str, Any]] = {
    "photography": {"category": "interest", "keywords": ["摄影", "拍照", "photo", "camera"]},
    "music": {"category": "interest", "keywords": ["音乐", "歌", "music"]},
    "nas": {"category": "topic", "keywords": ["nas", "存储", "硬盘", "volume", "dsm"]},
    "ai_dev": {"category": "topic", "keywords": ["ai", "模型", "llm", "训练", "开发", "deploy"]},
    "speed": {"category": "work_style", "keywords": ["快", "效率", "自动", "简化", "一键"]},
    "quality": {"category": "work_style", "keywords": ["稳定", "安全", "可靠", "健壮", "严谨"]},
    "minimalism": {"category": "work_style", "keywords": ["简洁", "精简", "不要冗余", "干净"]},
    "open_source": {"category": "value", "keywords": ["开源", "open source", "免费"]},
    "automation": {"category": "work_style", "keywords": ["自动", "cron", "ci", "流水线"]},
    "architecture": {"category": "topic", "keywords": ["架构", "设计模式", "ddd", "uml", "c4"]},
}


class PersonalityEngine:
    """Tracks and evolves user preferences over time.

    Preferences are discovered from conversation content and strengthen
    with each mention. Unmentioned preferences slowly decay.
    """

    def __init__(self):
        self._preferences: dict[str, Preference] = {}
        self._initialize_defaults()

    def _initialize_defaults(self) -> None:
        """Set initial preferences based on known user patterns."""
        for name, config in PREFERENCE_PATTERNS.items():
            self._preferences[name] = Preference(
                name=name,
                category=config["category"],
                score=INITIAL_SCORE,
                mention_count=0,
                first_seen=datetime.now(timezone.utc).isoformat(),
                last_seen=datetime.now(timezone.utc).isoformat(),
            )

    def process_text(self, text: str, source: str = "conversation") -> list[str]:
        """Process text and update relevant preferences.

        Returns list of preference names that were updated.
        """
        updated = []
        text_lower = text.lower()

        for name, pref in self._preferences.items():
            config = PREFERENCE_PATTERNS.get(name)
            if not config:
                continue

            # Check if any keywords match
            matched = [kw for kw in config["keywords"] if kw in text_lower]
            if matched:
                # Boost preference
                pref.score = min(MAX_SCORE, pref.score + BOOST_PER_MENTION)
                pref.mention_count += 1
                pref.last_seen = datetime.now(timezone.utc).isoformat()

                # Store example (first occurrence text, truncated)
                example = text[:120] if text else ""
                if example and (not pref.examples or pref.examples[-1] != example):
                    pref.examples.append(example)
                    if len(pref.examples) > 5:
                        pref.examples = pref.examples[-5:]

                updated.append(name)

        return updated

    def apply_decay(self) -> None:
        """Apply time decay to all preferences. Call daily."""
        for pref in self._preferences.values():
            pref.score = max(0.0, pref.score - DECAY_PER_DAY)
            if pref.score <= ARCHIVE_SCORE:
                continue  # Archived but kept for potential reactivation

    def get_active_preferences(self, min_score: float = 0.3) -> list[Preference]:
        """Get preferences above a threshold."""
        return sorted(
            [p for p in self._preferences.values() if p.score >= min_score],
            key=lambda p: p.score,
            reverse=True,
        )

    def get_summary(self) -> dict[str, Any]:
        """Get personality summary for prompt injection."""
        active = self.get_active_preferences()
        by_category: dict[str, list[dict]] = {}
        for pref in active:
            cat = pref.category
            if cat not in by_category:
                by_category[cat] = []
            by_category[cat].append({
                "name": pref.name,
                "score": round(pref.score, 2),
                "mention_count": pref.mention_count,
            })

        top = active[:3]
        return {
            "top_traits": [p.name for p in top],
            "confidence": [round(p.score, 2) for p in top],
            "by_category": by_category,
            "active_count": len(active),
            "total_tracked": len(self._preferences),
        }

    def to_dict(self) -> dict[str, Any]:
        """Full state dump."""
        return {
            "preferences": {
                name: {
                    "score": round(p.score, 2),
                    "category": p.category,
                    "mention_count": p.mention_count,
                    "last_seen": p.last_seen,
                    "examples": p.examples,
                }
                for name, p in self._preferences.items()
            },
            "summary": self.get_summary(),
        }


# Singleton
_personality_engine: PersonalityEngine | None = None


def get_personality_engine() -> PersonalityEngine:
    global _personality_engine
    if _personality_engine is None:
        _personality_engine = PersonalityEngine()
    return _personality_engine

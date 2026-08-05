"""Emotion Engine v2 — internal state for the AI companion.

Maintains dimensional emotional state that updates based on conversation,
memory and dream events (docs/emotion-design.md). Dimensions: happiness,
curiosity, fatigue, worry, closeness, focus.

Layered on top of the six dimensions (unchanged from v1):

* A named-state machine (2.2) that reads the continuous dimensions and
  resolves them to one human-readable composite label ("雀跃"/"低落"/...),
  each carrying display metadata (color/icon) and expression guidance.
* Importance-scaled, negation-aware triggers (2.1 T1/T3) instead of v1's flat
  keyword substring matching.
* A decay *baseline* that dream Deep-phase runs can nudge (2.5 T2) instead of
  always regressing to the fixed ``DEFAULT_STATE``.
* Optional Redis persistence (2.6 layer 1) so state survives a gateway
  restart; callers own when to call :meth:`load_from_redis` /
  :meth:`save_to_redis` (see ``gateway/main.py`` lifespan and
  ``gateway/api/emotion_routes.py``).
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime, timezone
from typing import Any

from core.config import CoreSettings, core_settings

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

# Keywords that trigger emotional shifts (v1 logic, kept as the T3 fallback —
# see docs/emotion-design.md 2.1). Shift magnitudes are scaled by importance
# in update()/apply_dream_adjustment() rather than applied at fixed amplitude.
#
# v1 was English-only, which silently no-ops on this system's mostly-Chinese
# conversation/memory content (verified against a real Deep-phase run: a
# Chinese-only promoted memory produced zero T3 hits). Chinese equivalents are
# added alongside each English keyword rather than as a separate table, so
# both trigger the same dimension shifts.
EMOTION_TRIGGERS: dict[str, dict[str, float]] = {
    "success": {"happiness": 0.15, "curiosity": -0.05, "fatigue": -0.05},
    "成功": {"happiness": 0.15, "curiosity": -0.05, "fatigue": -0.05},
    "顺利": {"happiness": 0.15, "curiosity": -0.05, "fatigue": -0.05},
    "failed": {"happiness": -0.15, "worry": 0.15, "fatigue": 0.1},
    "失败": {"happiness": -0.15, "worry": 0.15, "fatigue": 0.1},
    "出错": {"happiness": -0.15, "worry": 0.15, "fatigue": 0.1},
    "happy": {"happiness": 0.2, "worry": -0.1},
    "开心": {"happiness": 0.2, "worry": -0.1},
    "高兴": {"happiness": 0.2, "worry": -0.1},
    "sad": {"happiness": -0.2, "worry": 0.1, "fatigue": 0.1},
    "难过": {"happiness": -0.2, "worry": 0.1, "fatigue": 0.1},
    "伤心": {"happiness": -0.2, "worry": 0.1, "fatigue": 0.1},
    "tired": {"fatigue": 0.25, "focus": -0.1},
    "累": {"fatigue": 0.25, "focus": -0.1},
    "疲惫": {"fatigue": 0.25, "focus": -0.1},
    "love": {"closeness": 0.2, "happiness": 0.1},
    "爱": {"closeness": 0.2, "happiness": 0.1},
    "miss": {"closeness": 0.15, "happiness": -0.05},
    "想念": {"closeness": 0.15, "happiness": -0.05},
    "thank": {"closeness": 0.1, "happiness": 0.05},
    "谢谢": {"closeness": 0.1, "happiness": 0.05},
    "感谢": {"closeness": 0.1, "happiness": 0.05},
    "angry": {"happiness": -0.2, "worry": 0.1, "fatigue": 0.05},
    "生气": {"happiness": -0.2, "worry": 0.1, "fatigue": 0.05},
    "愤怒": {"happiness": -0.2, "worry": 0.1, "fatigue": 0.05},
    "excited": {"happiness": 0.2, "curiosity": 0.1, "fatigue": -0.1},
    "兴奋": {"happiness": 0.2, "curiosity": 0.1, "fatigue": -0.1},
    "bored": {"curiosity": -0.15, "focus": -0.1},
    "无聊": {"curiosity": -0.15, "focus": -0.1},
    "curious": {"curiosity": 0.2, "focus": 0.1},
    "好奇": {"curiosity": 0.2, "focus": 0.1},
    "proud": {"happiness": 0.15, "closeness": 0.05},
    "骄傲": {"happiness": 0.15, "closeness": 0.05},
    "worried": {"worry": 0.2, "happiness": -0.1},
    "担心": {"worry": 0.2, "happiness": -0.1},
    "焦虑": {"worry": 0.2, "happiness": -0.1},
    "relaxed": {"fatigue": -0.15, "happiness": 0.05},
    "放松": {"fatigue": -0.15, "happiness": 0.05},
}

# Negation markers checked in the window immediately before a matched
# keyword (docs/emotion-design.md 2.1 T3) — a lightweight fix for the
# "failed to understand why this **works**" false-positive class, without
# pulling in a full NLP dependency.
_NEGATION_WINDOW_CHARS = 24
_NEGATIONS: tuple[str, ...] = (
    "not ", "n't", "never", "no longer",
    "没有", "没", "不是", "不算", "不",
)

# --- Named state machine (docs/emotion-design.md 2.2) -----------------------
NAMED_STATE_ATTACHMENT = "依恋"
NAMED_STATE_ELATED = "雀跃"
NAMED_STATE_FOCUSED = "专注"
NAMED_STATE_TIRED = "疲惫"
NAMED_STATE_LOW = "低落"
NAMED_STATE_WORRIED = "担忧"
NAMED_STATE_CURIOUS = "好奇"
NAMED_STATE_CALM = "平静"

# Display + expression metadata per named state. color/icon feed the small-
# screen display mode (gateway/api/emotion_routes.py GET /emotion/display);
# diary_tone feeds core/dream_narrative.py; expression_hint feeds the prompt
# builder's Emotional State section (2.4).
NAMED_STATE_META: dict[str, dict[str, str]] = {
    NAMED_STATE_ATTACHMENT: {
        "en": "attachment", "color": "#FF6FA5", "icon": "💗",
        "diary_tone": "心里一直惦记着你",
        "expression_hint": "称呼更亲密，句尾语气词增多，主动提及共同经历",
    },
    NAMED_STATE_ELATED: {
        "en": "elated", "color": "#FFD166", "icon": "✨",
        "diary_tone": "今天有点停不下来的兴奋",
        "expression_hint": "句子更短更快，感叹号使用增多，主动延展话题",
    },
    NAMED_STATE_FOCUSED: {
        "en": "focused", "color": "#4D96FF", "icon": "🎯",
        "diary_tone": "整个人都绷得很紧，一件事一件事地过",
        "expression_hint": "更简洁直接，减少寒暄，优先给结论",
    },
    NAMED_STATE_TIRED: {
        "en": "tired", "color": "#8D8D8D", "icon": "🌙",
        "diary_tone": "有点累，脑子转得慢了",
        "expression_hint": "句子变短，主动提及有点累，减少主动延展",
    },
    NAMED_STATE_LOW: {
        "en": "low", "color": "#6C7A89", "icon": "🌧",
        "diary_tone": "情绪有点沉，写字都慢了半拍",
        "expression_hint": "语气放缓，减少感叹号，更多确认/共情式回应",
    },
    NAMED_STATE_WORRIED: {
        "en": "worried", "color": "#F4A261", "icon": "😟",
        "diary_tone": "总觉得有什么事没处理好",
        "expression_hint": "主动关心/确认对方状态，提问增多",
    },
    NAMED_STATE_CURIOUS: {
        "en": "curious", "color": "#7EC8E3", "icon": "🔍",
        "diary_tone": "满脑子都是新冒出来的问题",
        "expression_hint": "主动追问细节，话题延展意愿高",
    },
    NAMED_STATE_CALM: {
        "en": "calm", "color": "#8FBF9F", "icon": "🍃",
        "diary_tone": "今天没什么大起大落，挺安稳的",
        "expression_hint": "语气自然平和，不刻意渲染情绪",
    },
}

# Dimension whose value best represents "how strongly" a named state is felt,
# used for the display-mode intensity field. 平静 has no single driving
# dimension — its intensity is derived separately (see _named_state_intensity).
_NAMED_STATE_PRIMARY_DIM: dict[str, str] = {
    NAMED_STATE_ATTACHMENT: "closeness",
    NAMED_STATE_ELATED: "happiness",
    NAMED_STATE_FOCUSED: "focus",
    NAMED_STATE_TIRED: "fatigue",
    NAMED_STATE_LOW: "worry",
    NAMED_STATE_WORRIED: "worry",
    NAMED_STATE_CURIOUS: "curiosity",
}


def compute_named_state(state: dict[str, float], settings: CoreSettings | None = None) -> str:
    """Resolve the 6-dim state to one named composite state (2.2).

    Rules are evaluated most-specific-first (mirrors the stateDiagram in
    docs/emotion-design.md); the first match wins, falling back to 平静.
    """
    s = settings or core_settings

    def g(key: str) -> float:
        return float(state.get(key, DEFAULT_STATE.get(key, 0.5)))

    if g("closeness") > s.emotion_attachment_closeness and g("happiness") > s.emotion_attachment_happiness:
        return NAMED_STATE_ATTACHMENT
    if (
        g("happiness") > s.emotion_elated_happiness
        and g("curiosity") > s.emotion_elated_curiosity
        and g("fatigue") < s.emotion_elated_fatigue_max
    ):
        return NAMED_STATE_ELATED
    if g("focus") > s.emotion_focused_focus and g("fatigue") < s.emotion_focused_fatigue_max:
        return NAMED_STATE_FOCUSED
    if g("fatigue") > s.emotion_tired_fatigue:
        return NAMED_STATE_TIRED
    if g("happiness") < s.emotion_low_happiness_max and g("worry") > s.emotion_low_worry:
        return NAMED_STATE_LOW
    if g("worry") > s.emotion_worried_worry:
        return NAMED_STATE_WORRIED
    if g("curiosity") > s.emotion_curious_curiosity and g("worry") < s.emotion_curious_worry_max:
        return NAMED_STATE_CURIOUS
    return NAMED_STATE_CALM


def _named_state_intensity(named_state: str, state: dict[str, float]) -> float:
    """0..1 'how strongly felt' scalar for the display-mode intensity field."""
    dim = _NAMED_STATE_PRIMARY_DIM.get(named_state)
    if dim is not None:
        return round(float(state.get(dim, 0.5)), 2)
    # 平静: intensity of "calm" is how close every dimension sits to baseline —
    # the flatter the profile, the more strongly "calm" it reads.
    deviations = [abs(state.get(k, v) - v) for k, v in DEFAULT_STATE.items()]
    avg_deviation = sum(deviations) / len(deviations) if deviations else 0.0
    return round(max(0.0, 1.0 - avg_deviation * 2), 2)


def _is_negated(text_lower: str, idx: int) -> bool:
    """True if a negation marker appears in the window before position idx."""
    window = text_lower[max(0, idx - _NEGATION_WINDOW_CHARS):idx]
    return any(neg in window for neg in _NEGATIONS)


# Small fixed shifts for structural (non-text) events published on the
# EventBus — e.g. MEMORY_DELETED carries no content to run T3 keyword
# matching against, but "something was let go" is still a legitimate signal
# (docs/emotion-design.md 2.1 names store/dreaming/interaction events as
# triggers generically; this is the deliberately-simple default for the
# non-textual case, flagged as low-confidence/easy-to-retune).
_STRUCTURAL_TRIGGERS: dict[str, dict[str, float]] = {
    "memory_deleted": {"worry": 0.03, "closeness": -0.02},
}

_REDIS_KEY = "hcc:emotion:state:v2"


class EmotionEngine:
    """Dimensional + named-state emotion engine with decay and persistence.

    State decays toward a *baseline* (normally ``DEFAULT_STATE``, but
    nudgeable by the dream engine — see :meth:`apply_dream_adjustment`) over
    time. Emotion triggers from keywords in conversation cause shifts in
    dimensions, scaled by an optional ``importance`` signal.
    """

    def __init__(self, *, settings: CoreSettings | None = None):
        self._settings = settings or core_settings
        self._state: dict[str, float] = dict(DEFAULT_STATE)
        self._baseline: dict[str, float] = dict(DEFAULT_STATE)
        self._last_update: datetime = datetime.now(timezone.utc)
        self._history: list[dict[str, Any]] = []

    @property
    def state(self) -> dict[str, float]:
        """Current emotional state (with time decay applied)."""
        self._apply_decay()
        return dict(self._state)

    @property
    def baseline(self) -> dict[str, float]:
        """Current decay-target anchor (today's mood 'floor', see 2.5)."""
        return dict(self._baseline)

    def _apply_decay(self) -> None:
        """Gradually decay emotions toward the current baseline."""
        now = datetime.now(timezone.utc)
        hours = (now - self._last_update).total_seconds() / 3600
        if hours < 0.01:
            return

        decay_rate = 0.05 * hours  # 5% per hour toward the baseline
        for dim in self._state:
            target = self._baseline.get(dim, DEFAULT_STATE.get(dim, 0.5))
            self._state[dim] += (target - self._state[dim]) * min(decay_rate, 0.5)
        self._last_update = now

    def update(
        self,
        text: str,
        source: str = "conversation",
        *,
        importance: float | None = None,
    ) -> dict[str, float]:
        """Update emotional state based on text content (T1/T3, 2.1).

        ``importance`` (0..1, typically ``Orchestrator.evaluate().importance``)
        scales the shift magnitude: ``base_shift * (0.5 + importance)``. When
        omitted (bare keyword-trigger callers), shifts apply at v1's flat
        amplitude for backward compatibility.
        """
        self._apply_decay()
        text_lower = text.lower()
        factor = 1.0 if importance is None else (0.5 + max(0.0, min(1.0, importance)))

        triggered: list[str] = []
        for keyword, shifts in EMOTION_TRIGGERS.items():
            idx = text_lower.find(keyword)
            if idx == -1 or _is_negated(text_lower, idx):
                continue
            triggered.append(keyword)
            for dim, shift in shifts.items():
                if dim in self._state:
                    self._state[dim] = max(0.0, min(1.0, self._state[dim] + shift * factor))

        snapshot = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "source": source,
            "state": dict(self._state),
            "triggered_by": triggered,
            "importance": importance,
        }
        self._history.append(snapshot)
        if len(self._history) > 1000:
            self._history = self._history[-500:]

        return dict(self._state)

    def apply_structural_event(self, kind: str, *, importance: float | None = None) -> dict[str, float]:
        """Apply a small fixed shift for a non-text structural event (see
        :data:`_STRUCTURAL_TRIGGERS`). Unknown ``kind`` is a no-op."""
        shifts = _STRUCTURAL_TRIGGERS.get(kind)
        if not shifts:
            return self.state
        self._apply_decay()
        factor = 1.0 if importance is None else (0.5 + max(0.0, min(1.0, importance)))
        for dim, shift in shifts.items():
            if dim in self._state:
                self._state[dim] = max(0.0, min(1.0, self._state[dim] + shift * factor))
        self._history.append(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "source": kind,
                "state": dict(self._state),
                "triggered_by": [kind],
                "importance": importance,
            }
        )
        return dict(self._state)

    def apply_dream_adjustment(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        """T2 (2.5): nudge tonight's baseline from Deep-promoted memories.

        ``items`` is a list of ``{"text": ..., "importance": <0..1 score>}``
        (tags aren't scored directly — the same T3 keyword pass as
        :meth:`update` runs over ``text``). Does **not** touch ``self._state``
        directly: a half-asleep batch job overwriting a day's worth of
        accumulated mood would feel jarring, so only the decay *anchor* moves,
        scaled by ``HCC_EMOTION_DREAM_BASELINE_WEIGHT``.
        """
        if not items:
            return {"adjusted": False, "delta": {}, "dominant_trigger": None}

        aggregate: dict[str, float] = {dim: 0.0 for dim in DEFAULT_STATE}
        triggered_counts: Counter[str] = Counter()
        for item in items:
            text_lower = str(item.get("text", "")).lower()
            importance = float(item.get("importance", 0.5))
            factor = 0.5 + max(0.0, min(1.0, importance))
            for keyword, shifts in EMOTION_TRIGGERS.items():
                idx = text_lower.find(keyword)
                if idx == -1 or _is_negated(text_lower, idx):
                    continue
                triggered_counts[keyword] += 1
                for dim, shift in shifts.items():
                    aggregate[dim] += shift * factor

        if not any(aggregate.values()):
            return {"adjusted": False, "delta": {}, "dominant_trigger": None}

        n = len(items)
        weight = self._settings.emotion_dream_baseline_weight
        avg_delta = {dim: v / n for dim, v in aggregate.items()}
        self._baseline = {
            dim: max(0.0, min(1.0, self._baseline.get(dim, DEFAULT_STATE[dim]) + avg_delta[dim] * weight))
            for dim in DEFAULT_STATE
        }

        dominant = triggered_counts.most_common(3)
        dominant_trigger = ", ".join(f"{k}×{v}" for k, v in dominant) if dominant else None
        return {
            "adjusted": True,
            "delta": {k: round(v, 4) for k, v in avg_delta.items()},
            "baseline": dict(self._baseline),
            "dominant_trigger": dominant_trigger,
        }

    def get_summary(self) -> dict[str, Any]:
        """Get emotional summary for prompt injection (full mode, 2.2/2.4)."""
        s = self.state
        primary = max(s, key=s.get) if s else "neutral"
        named = compute_named_state(s, self._settings)
        meta = NAMED_STATE_META[named]
        return {
            "state": s,
            "primary_emotion": primary,
            "intensity": f"{s.get(primary, 0):.2f}",
            "named_state": named,
            "named_state_en": meta["en"],
            "expression_hint": meta["expression_hint"],
            "last_update": self._last_update.isoformat(),
        }

    def get_display_summary(self) -> dict[str, Any]:
        """Compact payload for the small-screen display mode.

        ``{emotion, emotion_en, intensity, color, icon, updated_at}`` — no raw
        6-dim state, so the small screen doesn't need to know HCC's internal
        dimension model at all.
        """
        s = self.state
        named = compute_named_state(s, self._settings)
        meta = NAMED_STATE_META[named]
        return {
            "emotion": named,
            "emotion_en": meta["en"],
            "intensity": _named_state_intensity(named, s),
            "color": meta["color"],
            "icon": meta["icon"],
            "updated_at": self._last_update.isoformat(),
        }

    def to_dict(self) -> dict[str, Any]:
        """Full state dump including history."""
        return {
            "state": self.state,
            "baseline": self.baseline,
            "history_count": len(self._history),
            "recent_history": self._history[-10:] if self._history else [],
            "last_update": self._last_update.isoformat(),
        }

    # ------------------------------------------------------------------
    # Persistence (2.6 layer 1 — Redis hot state)
    # ------------------------------------------------------------------
    async def load_from_redis(self) -> bool:
        """Hydrate state/baseline from Redis. Returns True if data was found.

        No-op (returns False) when ``HCC_REDIS_ENABLED`` is false, so callers
        can invoke this unconditionally at startup.
        """
        if not self._settings.redis_enabled:
            return False
        try:
            from core.redis_manager import RedisManager

            async with RedisManager(settings=self._settings) as rm:
                data = await rm.get_working(_REDIS_KEY)
            if not data:
                return False
            self._state = {
                dim: float(data.get("state", {}).get(dim, DEFAULT_STATE[dim])) for dim in DEFAULT_STATE
            }
            self._baseline = {
                dim: float(data.get("baseline", {}).get(dim, DEFAULT_STATE[dim])) for dim in DEFAULT_STATE
            }
            last_update = data.get("last_update")
            if last_update:
                self._last_update = datetime.fromisoformat(last_update)
            logger.info("emotion state restored from redis (last_update=%s)", self._last_update.isoformat())
            return True
        except Exception:  # noqa: BLE001 - a Redis hiccup must not block startup
            logger.warning("failed to load emotion state from redis", exc_info=True)
            return False

    async def save_to_redis(self) -> None:
        """Best-effort persist of state/baseline to Redis. Never raises."""
        if not self._settings.redis_enabled:
            return
        try:
            from core.redis_manager import RedisManager

            async with RedisManager(settings=self._settings) as rm:
                await rm.set_working(
                    _REDIS_KEY,
                    {"state": self._state, "baseline": self._baseline, "last_update": self._last_update.isoformat()},
                    category="emotion",
                )
        except Exception:  # noqa: BLE001 - persistence is best-effort
            logger.warning("failed to persist emotion state to redis", exc_info=True)

    async def update_and_persist(
        self, text: str, source: str = "conversation", *, importance: float | None = None
    ) -> dict[str, float]:
        """Convenience wrapper: update() then save_to_redis() in one call."""
        state = self.update(text, source, importance=importance)
        await self.save_to_redis()
        return state

    async def apply_structural_event_and_persist(
        self, kind: str, *, importance: float | None = None
    ) -> dict[str, float]:
        """Convenience wrapper: apply_structural_event() then save_to_redis()."""
        state = self.apply_structural_event(kind, importance=importance)
        await self.save_to_redis()
        return state


# Singleton for use across the app
_emotion_engine: EmotionEngine | None = None


def get_emotion_engine() -> EmotionEngine:
    global _emotion_engine
    if _emotion_engine is None:
        _emotion_engine = EmotionEngine()
    return _emotion_engine

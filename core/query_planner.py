"""Rule-based query planner for the HCC v2.1 context pipeline.

The :class:`QueryPlanner` inspects an incoming free-text query and decides
*which* retrieval providers/managers should be consulted, how the request
should be prioritised, and how long the resulting context may be cached. It is
the first stage of the ``POST /api/v1/context`` pipeline:

    QueryPlanner -> ContextBuilder -> PromptBuilder

The default strategy (``HCC_PLANNER_MODEL=rule-based``) needs no model: it
classifies the query with cheap keyword heuristics. The design intentionally
leaves room for a future model-backed planner selected via the same env var,
but the rule-based planner is always available as a zero-dependency fallback.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import Enum

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

__all__ = [
    "QueryType",
    "Plan",
    "PlannerSettings",
    "QueryPlanner",
]


class QueryType(str, Enum):
    """Coarse classification of an incoming query.

    The value drives which providers the :class:`ContextBuilder` should invoke
    and how aggressively the result may be cached.
    """

    MEMORY_SEARCH = "memory_search"
    KNOWLEDGE_SEARCH = "knowledge_search"
    CONTEXT_BUILD = "context_build"
    EMOTION_QUERY = "emotion_query"


@dataclass
class Plan:
    """The execution plan produced by :meth:`QueryPlanner.analyze`.

    Attributes
    ----------
    query_type:
        The detected :class:`QueryType`.
    providers_needed:
        Ordered list of provider/manager keys to invoke, e.g.
        ``["memory", "knowledge"]``. May also include ``"emotion"``.
    priority:
        Integer priority hint (higher = more urgent). Interactive
        context builds rank above single-source lookups.
    ttl:
        Suggested cache time-to-live in seconds for the assembled context.
    include_emotion:
        Whether the emotion/personality state should be folded in.
    reason:
        Human-readable explanation of the classification (for logging/debug).
    """

    query_type: QueryType
    providers_needed: list[str] = field(default_factory=list)
    priority: int = 0
    ttl: int = 1800
    include_emotion: bool = False
    reason: str = ""


class PlannerSettings(BaseSettings):
    """Planner configuration sourced from ``HCC_*`` env vars."""

    model_config = SettingsConfigDict(
        env_prefix="HCC_", env_file=".env", extra="ignore"
    )

    planner_model: str = Field(
        default="rule-based",
        description="Planner strategy selector (HCC_PLANNER_MODEL).",
    )
    ttl_chat: int = Field(
        default=1800,
        ge=1,
        description="Default TTL (s) applied to context builds (HCC_TTL_CHAT).",
    )


# Keyword sets used by the rule-based classifier. Kept lightweight and
# language-agnostic-ish (common English cues); order of checks matters.
_MEMORY_CUES = (
    "remember",
    "recall",
    "last time",
    "we discussed",
    "you said",
    "earlier",
    "previously",
    "my name",
    "who am i",
    "history",
)
_KNOWLEDGE_CUES = (
    "how do i",
    "how to",
    "what is",
    "what are",
    "explain",
    "definition",
    "docs",
    "documentation",
    "reference",
    "guide",
)
_EMOTION_CUES = (
    "feel",
    "feeling",
    "mood",
    "emotion",
    "how are you",
    "sad",
    "happy",
    "angry",
    "upset",
    "personality",
)


def _contains_any(text: str, cues: tuple[str, ...]) -> bool:
    """Return ``True`` if ``text`` contains any of the ``cues`` substrings."""
    return any(cue in text for cue in cues)


class QueryPlanner:
    """Classify queries and produce retrieval :class:`Plan` objects.

    Parameters
    ----------
    settings:
        Optional :class:`PlannerSettings`. Defaults to env-derived settings.
    """

    def __init__(self, *, settings: PlannerSettings | None = None) -> None:
        self._settings = settings or PlannerSettings()

    @property
    def model(self) -> str:
        """Return the active planner strategy identifier."""
        return self._settings.planner_model

    def analyze(self, query: str) -> Plan:
        """Analyse ``query`` and return an execution :class:`Plan`.

        The rule-based strategy applies keyword heuristics. When no single
        signal dominates (the common case for conversational turns), it falls
        back to a full ``context_build`` that consults every provider.

        Parameters
        ----------
        query:
            The raw free-text query.

        Returns
        -------
        Plan
            The retrieval plan describing providers, priority and TTL.
        """
        normalized = re.sub(r"\s+", " ", (query or "").strip().lower())
        default_ttl = self._settings.ttl_chat

        if not normalized:
            # Empty query -> browse recent memories only, do not cache long.
            return Plan(
                query_type=QueryType.MEMORY_SEARCH,
                providers_needed=["memory"],
                priority=1,
                ttl=min(default_ttl, 300),
                include_emotion=False,
                reason="empty query -> recent memory browse",
            )

        is_memory = _contains_any(normalized, _MEMORY_CUES)
        is_knowledge = _contains_any(normalized, _KNOWLEDGE_CUES)
        is_emotion = _contains_any(normalized, _EMOTION_CUES)

        # Emotion cue with no strong retrieval signal -> emotion-first query.
        if is_emotion and not (is_memory or is_knowledge):
            return Plan(
                query_type=QueryType.EMOTION_QUERY,
                providers_needed=["emotion", "memory"],
                priority=2,
                ttl=min(default_ttl, 300),
                include_emotion=True,
                reason="emotion cue dominant",
            )

        # Exactly one retrieval signal -> single-source lookup.
        if is_memory and not is_knowledge:
            return Plan(
                query_type=QueryType.MEMORY_SEARCH,
                providers_needed=["memory"],
                priority=2,
                ttl=default_ttl,
                include_emotion=is_emotion,
                reason="memory cue dominant",
            )
        if is_knowledge and not is_memory:
            return Plan(
                query_type=QueryType.KNOWLEDGE_SEARCH,
                providers_needed=["knowledge"],
                priority=2,
                ttl=default_ttl,
                include_emotion=is_emotion,
                reason="knowledge cue dominant",
            )

        # Mixed / ambiguous signal -> full context build (default path).
        providers = ["memory", "knowledge"]
        if is_emotion:
            providers.append("emotion")
        return Plan(
            query_type=QueryType.CONTEXT_BUILD,
            providers_needed=providers,
            priority=3,
            ttl=default_ttl,
            include_emotion=is_emotion,
            reason="mixed/ambiguous signal -> full context build",
        )

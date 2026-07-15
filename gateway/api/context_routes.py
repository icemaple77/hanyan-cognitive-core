"""Context assembly API — ``POST /api/v1/context``.

This route exposes the HCC v2.1 context pipeline over HTTP:

    QueryPlanner -> ContextBuilder -> PromptBuilder

Given a free-text ``query`` and ``user_id`` it (1) classifies the query to
decide which providers to consult, (2) fans out to the memory/knowledge
managers (and optional emotion provider) to gather context, and (3) assembles a
ready-to-use LLM prompt. The response reports the assembled context, the source
providers that contributed, and the final prompt with a token estimate.

The heavy objects (planner, builders, managers) are created once at import time
and reused across requests; all of them are stateless with respect to a single
call, so this is safe under FastAPI's async concurrency.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from core.config import core_settings
from core.managers.context_builder import ContextBuilder
from core.prompt_builder import PromptBuilder
from core.query_planner import QueryPlanner

logger = logging.getLogger(__name__)

router = APIRouter()

# Shared, stateless pipeline components (constructed once, reused per request).
_planner = QueryPlanner()
_context_builder = ContextBuilder()
_prompt_builder = PromptBuilder()


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class ContextRequest(BaseModel):
    """Request body for ``POST /api/v1/context``."""

    query: str = Field(..., description="Free-text query driving retrieval.")
    user_id: str = Field(..., description="Owner whose context is assembled.")
    agent_id: str = Field(default="default", description="Agent scope for retrieval.")
    include_shared: bool = Field(default=True, description="Include shared global memories.")
    include_emotion: bool = Field(
        default=False,
        description="Fold the emotional state into the context when available.",
    )
    include_personality: bool = Field(
        default=False,
        description="Fold the personality/persona into the context.",
    )
    limit: int = Field(
        default=core_settings.context_default_limit,
        ge=1,
        description="Per-provider item cap (clamped to HCC_CONTEXT_MAX_LIMIT).",
    )
    system_prompt: str | None = Field(
        default=None,
        description="Optional base system prompt to prepend to the built prompt.",
    )
    conversation: list[dict[str, Any]] | None = Field(
        default=None,
        description="Optional conversation transcript as role/content messages.",
    )


class SourceInfo(BaseModel):
    """One retrieval source that contributed to the assembled context."""

    provider: str
    type: str
    count: int


class ContextResponse(BaseModel):
    """Response body for ``POST /api/v1/context``."""

    context: str = Field(..., description="Human-readable assembled context.")
    sources: list[SourceInfo] = Field(
        default_factory=list, description="Providers that contributed context."
    )
    prompt: str = Field(..., description="The final assembled LLM prompt.")
    query_type: str = Field(..., description="Detected query classification.")
    token_count_estimate: int = Field(
        default=0, description="Approximate token count of the prompt."
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict, description="Plan + prompt assembly metadata."
    )


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------
@router.post("/context", response_model=ContextResponse)
async def build_context(request: ContextRequest) -> ContextResponse:
    """Plan, gather and assemble context + prompt for a query.

    Parameters
    ----------
    request:
        The :class:`ContextRequest` body.

    Returns
    -------
    ContextResponse
        The assembled context, contributing sources and final prompt.

    Raises
    ------
    HTTPException
        With status 500 if the pipeline fails unexpectedly.
    """
    limit = min(request.limit, core_settings.context_max_limit)

    # 1. Plan: classify the query and decide what to retrieve.
    plan = _planner.analyze(request.query)
    include_emotion = request.include_emotion or plan.include_emotion
    logger.info(
        "Context request user=%s type=%s providers=%s limit=%d",
        request.user_id,
        plan.query_type.value,
        plan.providers_needed,
        limit,
    )

    try:
        # 2. Build: fan out to memory/knowledge (+ optional emotion).
        context = await _context_builder.build(
            request.query,
            request.user_id,
            include_emotion=include_emotion,
            include_personality=request.include_personality,
            limit=limit,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Context build failed for user=%s", request.user_id)
        raise HTTPException(
            status_code=500, detail=f"Context build failed: {exc}"
        ) from exc

    sources = [
        SourceInfo(
            provider=src.get("provider", ""),
            type=src.get("type", ""),
            count=len(src.get("items", [])),
        )
        for src in context.get("sources", [])
    ]

    # Split the rendered context back into memory/knowledge blocks for the
    # prompt builder using the structured sources.
    memory_items, knowledge_items = _split_items(context.get("sources", []))

    # 3. Assemble: turn everything into a final prompt.
    try:
        built = _prompt_builder.build(
            system_prompt=request.system_prompt,
            conversation=request.conversation,
            memory_context=_render_items(memory_items, key="summary"),
            knowledge_context=_render_items(knowledge_items, key="heading"),
            emotion_state=context.get("emotion_state"),
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Prompt build failed for user=%s", request.user_id)
        raise HTTPException(
            status_code=500, detail=f"Prompt build failed: {exc}"
        ) from exc

    metadata: dict[str, Any] = {
        "plan": {
            "query_type": plan.query_type.value,
            "providers_needed": plan.providers_needed,
            "priority": plan.priority,
            "ttl": plan.ttl,
            "reason": plan.reason,
        },
        "prompt": built["metadata"],
        "provider_metadata": context.get("provider_metadata", {}),
    }

    return ContextResponse(
        context=context.get("context", ""),
        sources=sources,
        prompt=built["prompt"],
        query_type=plan.query_type.value,
        token_count_estimate=built["token_count_estimate"],
        metadata=metadata,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _split_items(
    sources: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (memory_items, knowledge_items) from the sources list."""
    memory_items: list[dict[str, Any]] = []
    knowledge_items: list[dict[str, Any]] = []
    for src in sources:
        if src.get("type") == "memory":
            memory_items = src.get("items", [])
        elif src.get("type") == "knowledge":
            knowledge_items = src.get("items", [])
    return memory_items, knowledge_items


def _render_items(items: list[dict[str, Any]], *, key: str) -> str:
    """Render item headlines into a bullet list for the prompt builder."""
    lines: list[str] = []
    for item in items:
        headline = (item.get(key) or item.get("content") or item.get("id") or "")
        headline = str(headline).strip().splitlines()[0] if headline else ""
        if headline:
            lines.append(f"- {headline}")
    return "\n".join(lines)

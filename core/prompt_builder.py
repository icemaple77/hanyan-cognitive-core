"""Assemble a structured LLM prompt from the HCC context components.

The :class:`PromptBuilder` is the final stage of the context pipeline:

    QueryPlanner -> ContextBuilder -> PromptBuilder

It takes the individually-retrieved pieces (system prompt, conversation,
memory context, knowledge context, emotional state, personality) and lays them
out into a single, clearly-sectioned prompt string that an LLM can consume
directly. Every section is optional; empty inputs are skipped so the resulting
prompt stays compact.

The builder is deliberately backend-agnostic and side-effect free: it performs
no I/O and no tokenizer imports, estimating token counts with a cheap
heuristic (~4 characters per token) that is good enough for budgeting and
observability without adding heavy dependencies.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

__all__ = ["PromptBuilder"]

# Rough average characters-per-token used for the size estimate. This matches
# the commonly-cited ~4 chars/token rule for English + code and is intended for
# budgeting, not exact accounting.
_CHARS_PER_TOKEN = 4


class PromptBuilder:
    """Compose the final prompt string from structured context components.

    Parameters
    ----------
    section_order:
        Optional override for the order in which sections are emitted. Unknown
        keys are ignored; omitted-but-present sections keep the default order.
    """

    #: Default top-to-bottom section ordering.
    DEFAULT_SECTION_ORDER: tuple[str, ...] = (
        "system",
        "personality",
        "emotion",
        "memory",
        "knowledge",
        "conversation",
    )

    def __init__(self, *, section_order: tuple[str, ...] | None = None) -> None:
        self._section_order = section_order or self.DEFAULT_SECTION_ORDER

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def build(
        self,
        *,
        system_prompt: str | None = None,
        conversation: list[dict[str, Any]] | str | None = None,
        memory_context: str | None = None,
        knowledge_context: str | None = None,
        emotion_state: dict[str, Any] | str | None = None,
        personality: dict[str, Any] | str | None = None,
    ) -> dict[str, Any]:
        """Assemble the components into a single structured prompt.

        Parameters
        ----------
        system_prompt:
            The base system/role instruction, placed first.
        conversation:
            Either a rendered transcript string or a list of
            ``{"role": ..., "content": ...}`` message dicts.
        memory_context:
            Pre-rendered relevant-memory text (e.g. from ``ContextBuilder``).
        knowledge_context:
            Pre-rendered knowledge-base text.
        emotion_state:
            Current emotional state as a dict or pre-rendered string.
        personality:
            Personality/persona description as a dict or string.

        Returns
        -------
        dict
            ``{"prompt": str, "token_count_estimate": int, "metadata": {...}}``
            where ``metadata`` reports which sections were included and their
            individual character counts.
        """
        rendered: dict[str, str] = {
            "system": self._clean(system_prompt),
            "personality": self._render_personality(personality),
            "emotion": self._render_emotion(emotion_state),
            "memory": self._section("Relevant Memories", self._clean(memory_context)),
            "knowledge": self._section("Knowledge", self._clean(knowledge_context)),
            "conversation": self._render_conversation(conversation),
        }

        blocks: list[str] = []
        included: list[str] = []
        section_chars: dict[str, int] = {}
        for key in self._section_order:
            text = rendered.get(key, "")
            if not text:
                continue
            blocks.append(text)
            included.append(key)
            section_chars[key] = len(text)

        prompt = "\n\n".join(blocks)
        token_estimate = self._estimate_tokens(prompt)

        metadata = {
            "sections_included": included,
            "section_char_counts": section_chars,
            "char_count": len(prompt),
            "chars_per_token": _CHARS_PER_TOKEN,
        }
        logger.debug(
            "Built prompt: %d chars, ~%d tokens, sections=%s",
            len(prompt),
            token_estimate,
            included,
        )
        return {
            "prompt": prompt,
            "token_count_estimate": token_estimate,
            "metadata": metadata,
        }

    # ------------------------------------------------------------------
    # Section renderers
    # ------------------------------------------------------------------
    @staticmethod
    def _clean(value: str | None) -> str:
        """Return a stripped string, or empty string for ``None``."""
        return (value or "").strip()

    @staticmethod
    def _section(heading: str, body: str) -> str:
        """Wrap ``body`` under a ``## heading`` block, or return ""."""
        body = (body or "").strip()
        if not body:
            return ""
        # Avoid double-heading if the body already leads with the heading.
        if body.lstrip().startswith("#"):
            return body
        return f"## {heading}\n{body}"

    def _render_personality(
        self, personality: dict[str, Any] | str | None
    ) -> str:
        """Render personality data into a ``## Personality`` section."""
        if not personality:
            return ""
        if isinstance(personality, str):
            return self._section("Personality", personality)
        lines = [f"- {k}: {v}" for k, v in personality.items() if v not in (None, "")]
        return self._section("Personality", "\n".join(lines))

    def _render_emotion(
        self, emotion_state: dict[str, Any] | str | None
    ) -> str:
        """Render emotion data into an ``## Emotional State`` section."""
        if not emotion_state:
            return ""
        if isinstance(emotion_state, str):
            return self._section("Emotional State", emotion_state)
        mood = emotion_state.get("mood") or emotion_state.get("state")
        lines: list[str] = []
        if mood:
            lines.append(f"- mood: {mood}")
        for key, value in emotion_state.items():
            if key in ("mood", "state") or value in (None, ""):
                continue
            lines.append(f"- {key}: {value}")
        return self._section("Emotional State", "\n".join(lines))

    def _render_conversation(
        self, conversation: list[dict[str, Any]] | str | None
    ) -> str:
        """Render the conversation transcript into a ``## Conversation`` block."""
        if not conversation:
            return ""
        if isinstance(conversation, str):
            return self._section("Conversation", conversation)
        lines: list[str] = []
        for message in conversation:
            role = str(message.get("role", "user")).strip() or "user"
            content = str(message.get("content", "")).strip()
            if not content:
                continue
            lines.append(f"{role}: {content}")
        return self._section("Conversation", "\n".join(lines))

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _estimate_tokens(text: str) -> int:
        """Estimate token count from character length (~4 chars/token)."""
        if not text:
            return 0
        return max(1, len(text) // _CHARS_PER_TOKEN)

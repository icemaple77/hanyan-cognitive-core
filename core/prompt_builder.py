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
    #:
    #: **按"易变程度"排,最稳的在前、最易变的在后** —— 这是为前缀缓存排的,
    #: 不是为可读性排的(2026-09-04,公子提醒「注入尾部不要影响缓存命中」)。
    #:
    #: 缓存吃的是**最长公共前缀**:某一块变了,它后面的全部作废。所以:
    #:   system / personality  几乎不变        → 最前
    #:   memory / knowledge    随 query 变,但一轮内稳定
    #:   conversation          **只增不改**——上一轮的文本原样保留,天然可缓存
    #:   emotion               **每轮都变**(16 维 + 语气指令 + 提醒)→ 必须最后
    #:
    #: 原顺序把 emotion 放在第 3 位,于是每轮情绪一动,后面的 memory + knowledge +
    #: conversation 全部掉出缓存 —— 而 conversation 通常是最大的一块。
    #: 把 emotion 挪到末尾后,可缓存前缀一直延伸到上一轮对话结尾。
    #:
    #: 顺带一个好处:行为指令放在最后,模型的近因效应反而让它更听话。
    DEFAULT_SECTION_ORDER: tuple[str, ...] = (
        "system",
        "personality",
        "memory",
        "knowledge",
        "conversation",
        "emotion",
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
        """Render emotion data into an ``## Emotional State`` section.

        Prefers the named-state label (docs/emotion-design.md 2.2) over the
        raw 6-dim ``state`` dict when both are present — "雀跃" is something a
        model can carry directly into tone, a dumped dict of floats is not
        (2.4). ``expression_hint`` (if present) rides along as a soft tone
        constraint via the generic key/value loop below.
        """
        if not emotion_state:
            return ""
        if isinstance(emotion_state, str):
            return self._section("Emotional State", emotion_state)
        mood = (
            emotion_state.get("mood")
            or emotion_state.get("named_state")
            or emotion_state.get("primary_emotion")
        )
        lines: list[str] = []
        if mood:
            lines.append(f"- mood: {mood}")

        # soul v2 的两样东西,比 17 个小数有用得多(2026-09-04,P5 接入):
        #
        # ① expression —— 语气指令。设计稿 §5.2:LLM 对「愉悦 0.73」反应很差,
        #    对「多用短句、少讲道理」反应很好。这是"情绪引导语气"真正落地的地方;
        #    没有它,情绪算得再准也只是几个不影响输出的数字。
        # ② reminder —— §六 提醒制,峰值回落后的"这段值得记下来"。
        #    公子定调:**只管发出,写不写是 agent 的事。**不挂待办、不等回执;
        #    情绪按半衰期退回基线,提醒自然消失——人类也是这样,回家累了就没写。
        #
        # 放在最前面:它们是行为约束,排在只读的维度读数之前。
        expression = emotion_state.get("expression")
        if expression:
            lines.append(f"- 表达:{expression}")
        reminder = emotion_state.get("reminder")
        if reminder:
            lines.append(f"- 记一笔:{reminder}")

        skip_keys = {"mood", "state", "named_state", "primary_emotion",
                     "expression", "reminder"}
        for key, value in emotion_state.items():
            if key in skip_keys or value in (None, ""):
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

"""Local-model memory noise filter (docs/local-noise-filter.md).

L1 review layer sitting behind ``core.orchestrator``'s L0 rule engine: calls a
local Ollama model to judge whether a piece of content is worth keeping as
long-term memory, for the "grey area" cases the keyword-matching L0 engine
can't reliably classify (see docs/local-noise-filter.md 零/四).

Never called synchronously from the write path — ``core.noise_filter_events``
drives this off the ``MEMORY_CREATED`` event, after the response has already
gone back to the client. On Ollama timeout/error this degrades to the L0
heuristic (``core.orchestrator``) rather than silently skipping review, so a
low-trust write still gets *some* verdict even when the local model is down.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass

import httpx

from core.config import core_settings
from core.orchestrator import get_orchestrator

logger = logging.getLogger(__name__)

__all__ = ["FilterDecision", "evaluate", "NOISE_FILTER_TAG", "DISCARDED_STATUS"]

# Applied to every memory the filter has reviewed (event-driven or backfill),
# live or backfilled — scripts/noise_filter_backfill.py uses it as its
# idempotency marker so reruns skip rows already reviewed by either path.
NOISE_FILTER_TAG = "noise_filter_v1:done"

# Soft-delete status for keep=false verdicts — distinct from forget.py's
# decay-driven "archived" (natural fade over time) so the two lifecycles
# don't get conflated; both are already excluded by every existing
# `Memory.status == "active"` retrieval filter since those whitelist active
# rather than blacklist archived/discarded.
DISCARDED_STATUS = "discarded"

PROMPT_TEMPLATE = """你是记忆库的存储质量守门员。判断下面这条内容是否值得作为长期记忆保存。

不值得存(keep=false)的例子：
- 工具调用的原始返回结果(文件读取内容、命令行输出、API 原始 JSON、会话列表/历史转储)
- 空结果或无信息量的状态确认("操作成功"、"messages: []")
- 日常寒暄、无实质内容的短句

值得存(keep=true)的例子：
- 用户的决定、偏好、事实性信息("我喜欢...","我们决定...")
- 项目进展、架构决策、bug修复方案、重要配置
- 情感表达、重要事件、承诺

只输出一行 JSON，不要任何解释：{{"keep": true或false, "importance": 0到1之间的浮点数}}

内容：
<<<{content}>>>"""

# Content-hash -> decision. OpenClaw tool logs repeat a lot of structurally
# identical noise ("messages": [] dumps, "Successfully replaced N block(s)"
# confirmations with only an id/path/timestamp varying) — caching on the exact
# content hash catches exact repeats without the cost/complexity of semantic
# similarity (docs/local-noise-filter.md 五).
_CACHE: dict[str, "FilterDecision"] = {}
_CACHE_MAX = 2000
_CACHE_TRIM_TO = 1500


@dataclass(frozen=True)
class FilterDecision:
    keep: bool
    importance: float
    source: str  # "model" | "heuristic_fallback"


def _cache_put(key: str, decision: FilterDecision) -> None:
    if len(_CACHE) >= _CACHE_MAX:
        # Evict oldest entries (insertion order) down to _CACHE_TRIM_TO,
        # mirroring the trim pattern already used by MemoryOrchestrator's
        # recent-content dedup window.
        for stale_key in list(_CACHE)[: len(_CACHE) - _CACHE_TRIM_TO]:
            _CACHE.pop(stale_key, None)
    _CACHE[key] = decision


async def evaluate(content: str, *, memory_source: str = "") -> FilterDecision:
    """Judge whether ``content`` is worth keeping as long-term memory.

    Tries the local Ollama model first; on any failure (timeout, connection
    error, malformed JSON) falls back to the existing rule-based
    ``core.orchestrator`` heuristic so the caller always gets a verdict.
    """
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    cached = _CACHE.get(content_hash)
    if cached is not None:
        return cached

    decision = await _call_ollama(content)
    if decision is None:
        decision = _heuristic_fallback(content, memory_source)

    _cache_put(content_hash, decision)
    return decision


async def _call_ollama(content: str) -> FilterDecision | None:
    truncated = content[: core_settings.noise_filter_truncate_chars]
    prompt = PROMPT_TEMPLATE.format(content=truncated)
    try:
        async with httpx.AsyncClient(timeout=core_settings.noise_filter_timeout) as client:
            resp = await client.post(
                f"{core_settings.noise_filter_ollama_url}/api/generate",
                json={
                    "model": core_settings.noise_filter_model,
                    "prompt": prompt,
                    "think": False,  # qwen3.5 is a hybrid-thinking model; without
                    # this it burns num_predict on <thinking> and "response" is empty.
                    "format": "json",
                    "stream": False,
                    "options": {"temperature": 0, "num_predict": 60},
                },
            )
            resp.raise_for_status()
            raw = resp.json().get("response", "")
            data = json.loads(raw)
        importance = max(0.0, min(1.0, float(data["importance"])))
        return FilterDecision(keep=bool(data["keep"]), importance=importance, source="model")
    except Exception:
        logger.warning("local_filter: Ollama evaluate failed, degrading to heuristic", exc_info=True)
        return None


def _heuristic_fallback(content: str, memory_source: str) -> FilterDecision:
    decision = get_orchestrator().evaluate(content, source=memory_source or "conversation")
    return FilterDecision(keep=decision.should_store, importance=decision.importance, source="heuristic_fallback")

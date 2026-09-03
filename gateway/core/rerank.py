"""Optional cross-encoder reranking for hybrid search (Qwen3-Reranker-0.6B GGUF).

Off by default (``HCC_RERANK_ENABLED=false``) — loading a 0.6B GGUF model
and running a forward pass per candidate is real latency (~tens of ms each)
that most callers of ``/memory/hybrid-search`` won't want by default. The RRF
fusion of BM25 + vector already gets most of the benefit; rerank is a knob
for callers who want the last bit of precision on the top-N and are willing
to pay for it.

Backend is llama-cpp-python loading the same GGUF QMD uses
(``~/.cache/qmd/models/qwen3-reranker-0.6b-q8_0.gguf``). The model's GGUF
metadata bakes in ``pooling_type=RANK``: a forward pass over the model's own
``tokenizer.chat_template.rerank`` prompt (a yes/no relevance judgement)
yields a 2-way softmax over {yes, no} in the first two output slots — we use
the "yes" probability as the relevance score. llama-cpp-python has no async
API, so calls are pushed to a thread via ``asyncio.to_thread``.

If llama-cpp-python isn't installed, or the model file is missing, or
loading/scoring fails, callers fall back to the RRF-only ranking — reranking
is a strict quality add-on, never a hard dependency of hybrid search.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

__all__ = ["RERANK_ENABLED", "is_available", "rerank"]
from core.config import core_settings  # 单一配置源(2026-09-03)

RERANK_ENABLED = core_settings.rerank_enabled
RERANK_MODEL_PATH = str(core_settings.rerank_model_path)
RERANK_N_CTX = core_settings.rerank_n_ctx

_llm_cache: dict = {}
_load_lock = asyncio.Lock()


def _get_llm():
    """Lazily load (and cache) the llama-cpp reranker model. Sync — call via to_thread."""
    if "llm" in _llm_cache:
        return _llm_cache["llm"]
    from llama_cpp import Llama  # local import: optional dependency

    if not os.path.exists(RERANK_MODEL_PATH):
        raise FileNotFoundError(f"rerank model not found: {RERANK_MODEL_PATH}")

    llm = Llama(model_path=RERANK_MODEL_PATH, embedding=True, n_ctx=RERANK_N_CTX, verbose=False)
    template = llm.metadata.get("tokenizer.chat_template.rerank")
    if not template:
        raise RuntimeError("rerank model GGUF has no tokenizer.chat_template.rerank metadata")
    _llm_cache["llm"] = llm
    _llm_cache["template"] = template
    return llm


def is_available() -> bool:
    """Best-effort check: can we even try to load the reranker? (Does not load it.)"""
    try:
        import llama_cpp  # noqa: F401
    except ImportError:
        return False
    return os.path.exists(RERANK_MODEL_PATH)


def _score_pair_sync(llm, template: str, query: str, document: str) -> float:
    prompt = template.format(query=query, document=document[:4000])
    out = llm.create_embedding(prompt)
    vals = out["data"][0]["embedding"]
    # First slot is the "yes" (relevant) softmax probability; RANK-pooling
    # output beyond the 2-class logits is unused padding from llama.cpp.
    return float(vals[0])


def _score_all_sync(query: str, documents: list[str]) -> list[float]:
    # One llama.cpp context, called sequentially in this single worker thread.
    # Its C++ state is NOT safe for concurrent calls from multiple threads —
    # scoring documents in parallel (asyncio.gather over separate to_thread
    # calls) segfaults. Latency is dominated by model load (~7s, cached after
    # the first call) plus ~tens of ms/doc, so sequential scoring of a
    # top-N candidate pool is still well within interactive budget.
    llm = _get_llm()
    template = _llm_cache["template"]
    return [_score_pair_sync(llm, template, query, doc) for doc in documents]


async def rerank(query: str, documents: list[str]) -> Optional[list[float]]:
    """Score each of ``documents`` against ``query``; higher = more relevant.

    Returns ``None`` (never raises) if the reranker is disabled or fails to
    load/run — callers should treat that as "rerank unavailable, keep RRF
    ordering" rather than an error.
    """
    if not documents:
        return []
    async with _load_lock:
        try:
            return await asyncio.to_thread(_score_all_sync, query, documents)
        except Exception:
            logger.warning("rerank scoring failed, falling back to RRF-only ranking", exc_info=True)
            return None

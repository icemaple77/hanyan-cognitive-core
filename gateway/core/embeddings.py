"""Text embedding provider with pluggable backends.

Supports:
- hash: deterministic hash-based (fallback, no model needed)
- ollama: Ollama embedding API (recommended for local use)
- sentence-transformers: local model (best quality, needs GPU)

Config via HCC_EMBEDDING_PROVIDER, HCC_EMBEDDING_MODEL, HCC_EMBEDDING_DIM.
"""

from __future__ import annotations

import hashlib
import math
import re

from dotenv import load_dotenv

# Unlike gateway.core.config.Settings (pydantic BaseSettings, which parses its
# own env_file), this module reads os.getenv() directly — nothing else in the
# process was loading .env into the real environment, so every HCC_EMBEDDING_*
# setting silently fell back to its hardcoded default (provider=hash) no
# matter what .env said. That's the actual root cause behind 体检报告's
# "Embedding 默认是 hash 后端" finding — this call is the fix, not just the
# .env value change. load_dotenv() no-ops quietly if no .env is found, and
# never overrides variables already set in the real environment.
load_dotenv()

__all__ = ["embed_text", "EMBEDDING_DIM"]

# Configuration —— 全部来自单一配置源 core_settings(2026-09-03 配置单一化)。
# 曾经这里各自 os.getenv,与 gateway/models 的硬编码维度分道扬镳,酿成向量维度
# 不符、文档语义检索静默全灭。现在建表维度与产出维度是同一个字段。
from core.config import core_settings

EMBEDDING_PROVIDER = core_settings.embedding_provider
EMBEDDING_MODEL = core_settings.embedding_model
EMBEDDING_DIM = core_settings.embedding_dim
OLLAMA_BASE_URL = core_settings.ollama_url
# BGE-family models want an asymmetric query instruction prepended to *queries*
# only (not to stored passages) for retrieval. Empty by default → no-op for
# ollama/hash and for symmetric models. For bge-*-zh set it to
# "为这个句子生成表示以用于检索相关文章：" in .env.
EMBEDDING_QUERY_INSTRUCTION = core_settings.embedding_query_instruction
# Pin sentence-transformers to CPU: the embedder sits on the memory hot path and
# must run *concurrently* with the brain's Metal generation. On CPU it runs truly
# parallel (no GPU contention → no repeat of the 2026-08 concurrent-Metal kernel
# panic, no Broker serialization latency). A 102M model embeds in ~14ms on CPU.
EMBEDDING_DEVICE = core_settings.embedding_device

def memory_embedding_text(content: str | None, summary: str | None = "") -> str:
    """一条记忆用于嵌入的**规范文本** —— 唯一定义,任何路径都必须走这里。

    2026-09-03 查证:08-29 的全库重嵌入按 ``content`` 单独算,而线上写入路径按
    ``content + summary`` 算,库里因此混着两套约定(重算余弦 0.92~0.99,不是空间
    错乱但确实不一致)。约定散落在多处 f-string 里就一定会漂,所以收成一个函数:
    写入、更新、重嵌入脚本共用,想改就只有这一处能改。
    """
    return f"{content or ''}\n{summary or ''}".strip()


def document_embedding_text(title: str | None, content: str | None) -> str:
    """一篇文档用于嵌入的规范文本(与 scripts/index_documents.py 的约定一致)。"""
    return f"{title or ''}\n{content or ''}".strip()


_TOKEN_RE = re.compile(r"\w+")

# Cache for loaded model
_model_cache: dict = {}


def embed_text(text: str, dim: int = EMBEDDING_DIM, is_query: bool = False) -> list[float]:
    """Embed text using the configured provider.

    is_query: when True and a query instruction is configured, prepend it (BGE
    asymmetric retrieval). Store-side callers leave it False; the query path in
    hybrid_search passes True.
    """
    if EMBEDDING_PROVIDER == "ollama":
        return _embed_ollama(text)
    elif EMBEDDING_PROVIDER == "sentence-transformers":
        return _embed_sentence(text, dim, is_query)
    else:
        return _embed_hash(text, dim)


def _embed_hash(text: str, dim: int) -> list[float]:
    """Deterministic hash-based embedding (fallback)."""
    vector = [0.0] * dim
    tokens = _TOKEN_RE.findall(text.lower())
    for token in tokens:
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        index = int.from_bytes(digest[:2], "big") % dim
        sign = 1.0 if digest[2] & 1 else -1.0
        vector[index] += sign
    norm = math.sqrt(sum(c * c for c in vector))
    if norm > 0.0:
        vector = [c / norm for c in vector]
    return vector


def _embed_ollama(text: str) -> list[float]:
    """Embed using Ollama API.

    P0-2 fix: on failure this now *raises* instead of silently returning a
    hash-space vector. The old fallback wrote a vector from a completely
    different embedding space into the same pgvector column that every other
    row treats as ollama-space — poisoning it so it could never again match
    semantically, with no marker to tell it apart. Worse, it defeated the
    null-embedding safety in MemoryService.create (which catches embed
    failures and stores NULL): create never saw the failure because this
    function swallowed it and handed back a plausible-looking vector.

    Callers that can tolerate a missing vector already catch this
    (MemoryService.create → stores NULL; hybrid_search → BM25-only). Let it
    propagate to them rather than corrupting the column. A deployment that
    genuinely wants the hash backend sets HCC_EMBEDDING_PROVIDER=hash, which
    routes here-around entirely.
    """
    import httpx

    resp = httpx.post(
        f"{OLLAMA_BASE_URL}/api/embeddings",
        json={"model": EMBEDDING_MODEL, "prompt": text},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["embedding"]


def _embed_sentence(text: str, dim: int, is_query: bool = False) -> list[float]:
    """Embed using sentence-transformers (CPU-resident; bge-base-zh by default).

    The model loads once into _model_cache and stays resident in-process — the
    gateway is always up, so the embedder is naturally always-resident. Pinned to
    CPU (see EMBEDDING_DEVICE) to run parallel to Metal generation without contention.
    """
    global _model_cache
    if "model" not in _model_cache:
        from sentence_transformers import SentenceTransformer
        _model_cache["model"] = SentenceTransformer(EMBEDDING_MODEL, device=EMBEDDING_DEVICE)
    model = _model_cache["model"]
    if is_query and EMBEDDING_QUERY_INSTRUCTION:
        text = EMBEDDING_QUERY_INSTRUCTION + text
    emb = model.encode(text, normalize_embeddings=True)
    return emb.tolist()

"""Text embedding provider with pluggable backends.

Supports:
- hash: deterministic hash-based (fallback, no model needed)
- ollama: Ollama embedding API (recommended for local use)
- sentence-transformers: local model (best quality, needs GPU)
- soul: 向 HanyanOS 的 soul 器官要向量,自己进程里不驻留模型(省 1.5G+,见 _embed_soul)

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
    if EMBEDDING_PROVIDER == "soul":
        return _embed_soul(text, is_query)
    elif EMBEDDING_PROVIDER == "ollama":
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


def _embed_soul(text: str, is_query: bool = False) -> list[float]:
    """向 soul 器官要向量 —— 模型只在 soul 的进程里驻留一份。

    为什么(公子 2026-09-04 问「HCC 为什么占 3G」之后选的方案 B):

      gateway 自己 import sentence_transformers 就意味着进程里长驻一整套
      torch。实测归因:torch 173MB + sentence_transformers 191MB + bge 权重
      27MB + 一次 encode 的工作区 +338MB + jieba 70MB ≈ 817MB 底子。更要命的
      是**批量 encode 的高水位不归还**:256 条 +537MB、150 篇长文 +730MB,
      del + gc.collect() 之后一分不退(torch 的分配器只还给自己的缓存池,
      不还给系统)。今天跑过全库重嵌入 + 文档重索引,于是 RSS 停在 2.4G。

      而 soul 进程里本来就驻留着**同一个** bge(它的冻结骨干)。两份同模型
      是纯浪费。骨干只由 soul 持有,gateway 走 HTTP 要向量,于是:
        - gateway 回到 200~300MB,再不会被批量任务顶起来
        - 少一份 bge
      这也正是 P0-a「共用同一份骨干 → 增量只有 6MB」那条结论铺的路,
      parity 已经验过(余弦 0.999999881)。

    ⚠️ 三条**不能省**的一致性检查,理由和 _embed_ollama 那段警告是同一条:
    往同一个 pgvector 列里写来自另一个向量空间的向量会永久污染它。所以

      1. 查询指令**在这一侧拼**(和 _embed_sentence 完全一样的时机),
         端点故意不接 is_query —— 指令在两边都拼就一定会漂
      2. 模型名不符就 raise。soul 哪天换了骨干,宁可让写入落 NULL,
         也不能悄悄混进第二个空间(vector_guard 正是查这个)
      3. 维度不符就 raise

    失败时**抛异常**,不退化成别的后端:MemoryService.create 会接住并存 NULL,
    hybrid_search 会退成纯 BM25 —— 这是既有的正确姿势。
    """
    import httpx

    if is_query and EMBEDDING_QUERY_INSTRUCTION:
        text = EMBEDDING_QUERY_INSTRUCTION + text

    resp = httpx.post(
        f"{core_settings.soul_service_url.rstrip('/')}/soul/embed",
        json={"texts": [text]},
        timeout=30,   # 不用 soul_service_timeout(2s):那是给情绪感知的,
                      # 感知可以降级,嵌入降级就是丢向量
    )
    resp.raise_for_status()
    data = resp.json()

    model = data.get("model")
    if model != EMBEDDING_MODEL:
        raise RuntimeError(
            f"soul 报的骨干是 {model!r},HCC 这一列是 {EMBEDDING_MODEL!r} —— "
            "拒绝写入,否则同一列里会混进两个向量空间"
        )
    vectors = data.get("vectors") or []
    if len(vectors) != 1 or len(vectors[0]) != EMBEDDING_DIM:
        raise RuntimeError(
            f"soul 返回 {len(vectors)} 条 / {len(vectors[0]) if vectors else 0} 维,"
            f"期望 1 条 / {EMBEDDING_DIM} 维"
        )
    return vectors[0]


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

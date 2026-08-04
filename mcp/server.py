#!/usr/bin/env python3
"""HCC MCP Memory Server.

Exposes HCC(记忆/情绪/人格/遗忘/知识)的核心能力为 MCP 工具,给支持 MCP
协议的 agent 框架接入 —— OpenClaw(HTTP注册外部MCP服务器)、Claude Code
(stdio)等都是标准 MCP 客户端,不需要给每个框架单独写一套接口。

2026-08 重写(见 memory_tools.py 顶部说明):原版依赖已不存在的旧版
``mcp.server.fastmcp`` import 路径 + 一套不兼容的伪嵌入,从没真正跑通过。
这版基于装好的 mcp==1.28.1(实测 FastMCP API —— ``@mcp.tool()``、
``mcp.settings.host/port``、``mcp.run(transport=...)`` —— 在 1.28.1 上
和文档一致,不需要再升到 1.29.0;pyproject 里的约束是 ``mcp>=1.2.0``。
2.0 是刚发布的破坏性重构版,生态里绝大多数示例/客户端还没跟上,不用。

用法::

    # stdio(Claude Code 等本地客户端直接拉起子进程)
    python mcp/server.py --transport stdio

    # streamable-http(OpenClaw 等注册一个 URL 的客户端)
    python mcp/server.py --transport streamable-http --host 0.0.0.0 --port 8001

同一份工具代码,两种传输方式,不用维护两套实现。

IMPORTANT — 为什么直接跑这个文件(``mcp/server.py``)而不是
``python -m mcp.server``:

    项目里有本地 ``mcp/`` 目录,又依赖第三方 ``mcp`` PyPI 包。用
    ``python -m mcp.server`` 跑会把当前目录塞进 sys.path 最前面,本地
    ``mcp`` 目录会挡住装好的第三方包,导致 ``from mcp.server.fastmcp
    import FastMCP`` 失败。直接跑这个文件只会把本文件所在目录(mcp/)
    加进 sys.path,不会加项目根目录,所以 ``import mcp`` 能正确解析到
    装好的第三方包,``gateway``/``core`` 仍可通过项目根的可编辑安装
    正常导入。下面再显式把项目根从 sys.path 摘掉,双重保险。
"""

from __future__ import annotations

import argparse
import os
import sys

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_THIS_DIR)
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)
# 项目根(/app)此时故意不在 sys.path 里 —— 如果它在,"import mcp" 会解析到
# /app/mcp/(本地目录本身),把装好的第三方 mcp 包顶替掉。等下面这行
# import 真正的 mcp 包成功、被 Python 缓存之后,再把项目根加回 sys.path,
# 这样后面 import memory_tools(它要 import gateway/core)才能找到东西,
# 且不会反过来影响已经缓存好的 mcp 包解析。
from mcp.server.fastmcp import FastMCP  # noqa: E402  (import after path fix)

if _PROJECT_ROOT not in sys.path:
    sys.path.append(_PROJECT_ROOT)

import memory_tools  # noqa: E402

mcp = FastMCP("hcc-memory")


@mcp.tool()
async def store_memory(
    content: str,
    user_id: str = "default",
    agent_id: str = "default",
    type: str = "general",
    summary: str = "",
    importance: float = 0.5,
    tags: list | None = None,
    embedding: list[float] | None = None,
) -> dict:
    """Store a new memory in HCC.

    Args:
        content: The memory text to store (required).
        user_id: Owner of the memory (e.g. "michael").
        agent_id: Which agent this memory belongs to (e.g. "hanyan", "hermes", "openclaw-main").
            Memories are scoped by agent_id — different agents don't see each other's memories
            unless explicitly queried across agents.
        type: Memory category, e.g. "general", "knowledge", "fact", "preference".
        summary: Optional short summary of the content.
        importance: Relevance score in [0, 1]. Defaults to 0.5.
        tags: Optional list of string tags.
        embedding: Optional precomputed embedding vector (1024-dim, Qwen3-Embedding-0.6B).
            HCC itself has no GPU/embedding model — pass this if your client can compute
            embeddings, otherwise this memory will only be findable via keyword/recall search.
    """
    return await memory_tools.store_memory(
        content=content, user_id=user_id, agent_id=agent_id, type=type,
        summary=summary, importance=importance, tags=tags, embedding=embedding,
    )


@mcp.tool()
async def search_memories(
    query: str,
    user_id: str | None = None,
    agent_id: str | None = None,
    type: str | None = None,
    limit: int = 20,
) -> dict:
    """Keyword-search memories (case-insensitive substring match on content/summary).

    Args:
        query: Substring to search for (required).
        user_id: Restrict to a specific user.
        agent_id: Restrict to a specific agent's memories.
        type: Restrict to a specific memory type.
        limit: Max number of results (1-100). Defaults to 20.
    """
    return await memory_tools.search_memories(
        query=query, user_id=user_id, agent_id=agent_id, type=type, limit=limit
    )


@mcp.tool()
async def recall(
    query: str,
    user_id: str | None = None,
    agent_id: str | None = None,
    limit: int = 5,
) -> dict:
    """Three-layer memory retrieval (conscious/preconscious/subconscious).

    Better than plain keyword search for "what do I remember about X" — merges
    current-session context with database recall, ranked by relevance.

    Args:
        query: What to recall.
        user_id: Restrict to a specific user.
        agent_id: Restrict to a specific agent's memories.
        limit: Max number of results. Defaults to 5.
    """
    return await memory_tools.recall(query=query, user_id=user_id, agent_id=agent_id, limit=limit)


@mcp.tool()
async def semantic_search(
    embedding: list[float],
    user_id: str | None = None,
    agent_id: str | None = None,
    type: str | None = None,
    limit: int = 10,
) -> dict:
    """Semantic-similarity search over stored memories (pgvector cosine distance).

    Requires a precomputed embedding vector — HCC has no GPU/embedding model itself.
    Compute the query embedding on your side (Qwen3-Embedding-0.6B, 1024-dim) and pass it here.

    Args:
        embedding: Query embedding vector (required, 1024-dim).
        user_id: Restrict to a specific user.
        agent_id: Restrict to a specific agent's memories.
        type: Restrict to a specific memory type (e.g. "knowledge" for the Obsidian vault).
        limit: Max number of results (1-100). Defaults to 10.
    """
    return await memory_tools.semantic_search(
        embedding=embedding, user_id=user_id, agent_id=agent_id, type=type, limit=limit
    )


@mcp.tool()
async def hybrid_search(
    query: str = "",
    embedding: list[float] | None = None,
    user_id: str | None = None,
    agent_id: str | None = None,
    type: str | None = None,
    limit: int = 10,
    rerank: bool = False,
) -> dict:
    """Hybrid search: BM25 full-text + vector similarity, fused with Reciprocal Rank Fusion.

    Best default choice for "find memories about X" — combines exact keyword
    matches (BM25, no embedding needed) with semantic similarity (if you pass
    an embedding), so it doesn't miss relevant memories that use different
    words than the query. Provide at least one of query/embedding; providing
    only one degrades gracefully to pure-BM25 or pure-vector search.

    Args:
        query: Free-text query for the BM25 branch (jieba-segmented server-side,
            works for mixed Chinese/English content). Optional.
        embedding: Precomputed query embedding (1024-dim, Qwen3-Embedding-0.6B)
            for the vector branch. Optional.
        user_id: Restrict to a specific user.
        agent_id: Restrict to a specific agent's memories.
        type: Restrict to a specific memory type.
        limit: Max number of results (1-100). Defaults to 10.
        rerank: Rerank the fused top results with the optional cross-encoder
            (Qwen3-Reranker-0.6B). Off by default — adds latency; silently
            falls back to RRF order if the reranker isn't enabled/available
            server-side (HCC_RERANK_ENABLED).
    """
    return await memory_tools.hybrid_search(
        query=query, embedding=embedding, user_id=user_id, agent_id=agent_id,
        type=type, limit=limit, rerank=rerank,
    )


@mcp.tool()
async def get_recent_memories(
    limit: int = 20,
    user_id: str | None = None,
    agent_id: str | None = None,
) -> dict:
    """Return the most recently created memories.

    Args:
        limit: Max number of results (1-100). Defaults to 20.
        user_id: Restrict to a specific user.
        agent_id: Restrict to a specific agent's memories.
    """
    return await memory_tools.get_recent_memories(limit=limit, user_id=user_id, agent_id=agent_id)


@mcp.tool()
async def delete_memory(memory_id: str) -> dict:
    """Delete a memory by its id (permanent — use forget/apply via the REST API for
    reversible archiving instead if you just want it to fade, not vanish).

    Args:
        memory_id: The id of the memory to delete (required).
    """
    return await memory_tools.delete_memory(memory_id=memory_id)


@mcp.tool()
async def evaluate(content: str, agent_id: str = "default", user_id: str = "default") -> dict:
    """Ask HCC's orchestrator whether a piece of content is worth remembering, before storing it.

    Use this to avoid flooding long-term memory with trivial chatter — only call
    store_memory for content where should_store comes back true (or when you have
    an explicit reason to override, e.g. the user said "remember this").

    Args:
        content: The text to evaluate.
        agent_id: Which agent is asking (for future per-agent tuning).
        user_id: Whose content this is.
    """
    return await memory_tools.evaluate(content=content, agent_id=agent_id, user_id=user_id)


def main() -> None:
    parser = argparse.ArgumentParser(description="HCC MCP Memory Server")
    parser.add_argument("--transport", choices=["stdio", "streamable-http", "sse"], default="stdio",
                       help="stdio for local subprocess clients (Claude Code); "
                            "streamable-http for network clients (OpenClaw registers a URL)")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8001)
    args = parser.parse_args()

    if args.transport == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.settings.host = args.host
        mcp.settings.port = args.port
        from mcp.server.transport_security import TransportSecuritySettings
        mcp.settings.transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[f"{args.host}:*"],
        )
        mcp.run(transport=args.transport)


if __name__ == "__main__":
    main()

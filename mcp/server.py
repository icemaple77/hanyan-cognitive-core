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
from mcp.server.fastmcp import FastMCP

if _PROJECT_ROOT not in sys.path:
    sys.path.append(_PROJECT_ROOT)

import memory_tools
import task_tools
import priority_tools

from core.config import core_settings

mcp = FastMCP("hcc-memory")

# P3-3: 三方共享记忆的 agent_id 规范 —— openclaw 用 "openclaw"(插件侧
# resolveConfig 已经这样做),hermes 用 "hermes"(~/.hermes/plugins/hcc 已经
#这样做),Claude Code(本 MCP server)理应用 "claude-code"。之前这里的默认值
# 是字面量 "default" —— 除非调用方每次都显式传 agent_id,写入的记忆就落不到
# claude-code 名下,三方就对不上号。这里改成读环境变量,由 MCP 客户端配置
# (~/.claude.json 的 mcpServers.hcc.env.HCC_AGENT_ID)按运行时身份注入,不用
# 依赖模型每次记得传参。
_DEFAULT_AGENT_ID = core_settings.agent_id


@mcp.tool()
async def store_memory(
    content: str,
    user_id: str = "default",
    agent_id: str = _DEFAULT_AGENT_ID,
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
        embedding: Deprecated, ignored. The server always computes its own embedding
            (ollama, server-side) so every memory lands in the same vector space —
            kept only so old callers that still pass one don't break.
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
    query: str = "",
    embedding: list[float] | None = None,
    user_id: str | None = None,
    agent_id: str | None = None,
    type: str | None = None,
    limit: int = 10,
) -> dict:
    """Semantic-similarity search over stored memories (pgvector cosine distance).

    Pass free-text `query` — the server embeds it (ollama, server-side) before
    searching. `embedding` remains available for passing a precomputed vector
    directly, but is no longer required.

    Args:
        query: Free-text query, embedded server-side. Required unless `embedding` is given.
        embedding: Precomputed query embedding vector, advanced/optional.
        user_id: Restrict to a specific user.
        agent_id: Restrict to a specific agent's memories.
        type: Restrict to a specific memory type (e.g. "knowledge" for the Obsidian vault).
        limit: Max number of results (1-100). Defaults to 10.
    """
    return await memory_tools.semantic_search(
        query=query, embedding=embedding, user_id=user_id, agent_id=agent_id, type=type, limit=limit
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
    matches (BM25) with semantic similarity, so it doesn't miss relevant
    memories that use different words than the query (e.g. "显卡" vs "GPU").
    Passing just `query` runs both branches — the server embeds the query text
    itself (ollama, server-side) for the vector branch, no client-side
    embedding model needed. Provide at least one of query/embedding.

    Args:
        query: Free-text query. Drives the BM25 branch (jieba-segmented
            server-side) and, unless `embedding` is given, is also embedded
            server-side for the vector branch. Optional if embedding is given.
        embedding: Precomputed query embedding for the vector branch, advanced/
            optional — normally you just pass `query` and let the server embed it.
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
async def evaluate(content: str, agent_id: str = _DEFAULT_AGENT_ID, user_id: str = "default") -> dict:
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


# ---------------------------------------------------------------------------
# Task-Schedule tools — agent long-task anti-stall (看板卡 t_6b29b140)
# ---------------------------------------------------------------------------
@mcp.tool()
async def task_create(
    title: str,
    steps: list[dict] = None,
    goal: str = "",
    user_id: str = "michael",
    agent_id: str = "default",
    redline_tags: list[str] = None,
    repeat: str = None,
) -> dict:
    """Register a long task so it survives session/compaction and gets driven to
    completion by external wakes instead of stalling after step one.

    Decompose the task into ordered steps up front. Each step is a dict:
      {"title": str, "instruction": str, "verify_cmd": str, "est_seconds": int}
    - instruction: what to DO this step.
    - verify_cmd: a shell command a fresh woken session runs to check progress
      deterministically (e.g. "test -f out.mp4 && echo done"). Don't rely on
      self-reported progress — a woken session has no memory of the prior run.
    - est_seconds: expected duration; drives the next wake interval. Pass 0 to
      let the server calibrate from history.
    After registering, do step 0 now, then call task_report for it.

    Args:
        title: Short task name.
        steps: Ordered list of step dicts (see above).
        goal: The overall objective, injected into every wake.
        user_id: Owner.
        agent_id: Which agent owns/drives this task (hermes / openclaw / ...).
        redline_tags: Extra keywords that force human escalation for this task.
        repeat: 循环任务(every:6h / daily:09:00 / 纯秒数);None=一次性。
    """
    return await task_tools.task_create(
        title=title, steps=steps or [], goal=goal,
        user_id=user_id, agent_id=agent_id, redline_tags=redline_tags, repeat=repeat,
    )


@mcp.tool()
async def task_get(task_id: str) -> dict:
    """Fetch a task and all its steps (status, current_step, estimates, attempts)."""
    return await task_tools.task_get(task_id=task_id)


@mcp.tool()
async def task_due(agent_id: str = None, limit: int = 20) -> dict:
    """List tasks whose wake is due now. The per-runtime cron polls this, then
    opens a fresh session per task and calls task_wake there.

    Args:
        agent_id: Restrict to one agent's tasks (recommended for a per-runtime cron).
        limit: Max tasks to return.
    """
    return await task_tools.task_due(agent_id=agent_id, limit=limit)


@mcp.tool()
async def task_wake(task_id: str) -> dict:
    """Get the current step's marching orders (call from a freshly woken session).

    Returns a `prompt` to act on and the `verify_cmd` to run. `action` is:
    "work" (do the step), "escalate" (blocked — ask the human via Feishu/微信),
    or "none" (task already terminal). Each call counts as one wake attempt;
    over the cap the task auto-blocks and escalates.
    """
    return await task_tools.task_wake(task_id=task_id)


@mcp.tool()
async def task_report(
    task_id: str,
    step_idx: int,
    verified_done: bool,
    actual_seconds: int = None,
    note: str = "",
) -> dict:
    """Report the DETERMINISTIC result of the current step, after running its
    verify_cmd. Do not guess — run the command and report what it showed.

    verified_done=True → the step is complete; advances to the next step (or
    finishes the task). False → re-estimates and reschedules the next wake so the
    same session (or a later woken one) keeps pushing. Pass actual_seconds on
    completion to feed the server's time calibration.

    Args:
        task_id: The task.
        step_idx: Which step you're reporting (must be the current step).
        verified_done: True only if verify_cmd showed the step is actually done.
        actual_seconds: How long the step really took (for calibration).
        note: Optional progress/blocker note stored on the task.
    """
    return await task_tools.task_report(
        task_id=task_id, step_idx=step_idx, verified_done=verified_done,
        actual_seconds=actual_seconds, note=note,
    )


@mcp.tool()
async def task_cancel(task_id: str) -> dict:
    """Cancel a task and stop all its future wakes."""
    return await task_tools.task_cancel(task_id=task_id)


# ── Priority Compass:公子的价值坐标(重要性×紧急性,跨运行时共享)─────────────
@mcp.tool()
async def priority_set(
    label: str,
    importance: int = 3,
    urgency: int = 3,
    anchors: list[str] = None,
    source: str = "agent",
    trust: str = None,
    review_at: str = None,
    user_id: str = "michael",
) -> dict:
    """登记一条价值坐标(重要性×紧急性各 1-5)。公子说「最近 X 最要紧」时调这个。

    agent 提案默认落 pending(半权隔离生效,不污染全局),需公子 priority_confirm
    转正。anchors 是主题锚词(如 ["肩颈","养伤","复诊"]),读路命中即给相关记忆加成。
    review_at(ISO 日期)过期 7 天未复核 α 自动减半。价值读时算,登记后无需回刷。
    """
    return await priority_tools.priority_set(
        label=label, importance=importance, urgency=urgency, anchors=anchors,
        source=source, trust=trust, review_at=review_at, user_id=user_id,
    )


@mcp.tool()
async def priority_list(user_id: str = "michael", status: str = "active") -> dict:
    """列出价值坐标(默认只列 active,带象限 Q1-Q4 与 α)。status=null 列全部。"""
    return await priority_tools.priority_list(user_id=user_id, status=status)


@mcp.tool()
async def priority_confirm(priority_id: str) -> dict:
    """把 pending 提案转正为 confirmed(全权重)。公子一句"转正"走这。"""
    return await priority_tools.priority_confirm(priority_id=priority_id)


@mcp.tool()
async def priority_retire(priority_id: str, superseded_by: str = None) -> dict:
    """退役一条价值坐标(不物删,记版本链)。事情办完/不再要紧时调。"""
    return await priority_tools.priority_retire(priority_id=priority_id, superseded_by=superseded_by)


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

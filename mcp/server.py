#!/usr/bin/env python3
"""HCC MCP Memory Server.

Exposes the Hanyan Cognitive Core memory store as MCP tools over the stdio
transport (for Claude Code / other MCP clients). It talks to the *same*
PostgreSQL database as the gateway API by reusing ``gateway.core.database``.

Launch it with::

    uv run python mcp/server.py

IMPORTANT — why we run it as a script (``mcp/server.py``) and NOT
``python -m mcp.server``:

    This project has a local ``mcp/`` directory *and* depends on the
    third-party ``mcp`` PyPI package. Running ``python -m mcp.server`` puts the
    current working directory on ``sys.path`` first, so the local ``mcp``
    package shadows the installed one and ``from mcp.server.fastmcp import
    FastMCP`` fails. Running the file directly only adds ``mcp/`` (this file's
    directory) to ``sys.path`` — not the project root — so ``import mcp``
    correctly resolves to the installed package while ``gateway`` stays
    importable via its editable install. As a belt-and-braces measure we also
    strip the project root from ``sys.path`` below.
"""

from __future__ import annotations

import os
import sys

# --- Import-path hardening -------------------------------------------------
# Ensure this file's directory (mcp/) is importable so ``memory_tools`` can be
# imported as a top-level module, and make sure the project root is NOT on the
# path (which would shadow the installed ``mcp`` package with our local dir).
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_THIS_DIR)
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)
sys.path[:] = [p for p in sys.path if os.path.abspath(p or os.getcwd()) != _PROJECT_ROOT]

from mcp.server.fastmcp import FastMCP  # noqa: E402  (import after path fix)

import memory_tools  # noqa: E402

mcp = FastMCP("hcc-memory")


@mcp.tool()
async def store_memory(
    content: str,
    user_id: str = "default",
    type: str = "general",
    summary: str = "",
    importance: float = 0.5,
    tags: list | None = None,
) -> dict:
    """Store a new memory in the HCC memory store.

    Args:
        content: The memory text to store (required).
        user_id: Owner of the memory. Defaults to "default".
        type: Memory category, e.g. "general", "fact", "preference".
        summary: Optional short summary of the content.
        importance: Relevance score in [0, 1]. Defaults to 0.5.
        tags: Optional list of string tags.
    """
    return await memory_tools.store_memory(
        content=content,
        user_id=user_id,
        type=type,
        summary=summary,
        importance=importance,
        tags=tags or [],
    )


@mcp.tool()
async def search_memories(
    query: str,
    user_id: str | None = None,
    type: str | None = None,
    limit: int = 20,
) -> dict:
    """Keyword-search memories by matching content/summary (case-insensitive).

    Args:
        query: Substring to search for (required).
        user_id: Restrict to a specific user.
        type: Restrict to a specific memory type.
        limit: Max number of results (1-100). Defaults to 20.
    """
    return await memory_tools.search_memories(
        query=query, user_id=user_id, type=type, limit=limit
    )


@mcp.tool()
async def semantic_search(
    query_text: str,
    limit: int = 10,
    user_id: str | None = None,
) -> dict:
    """Semantic-similarity search over stored memories (pgvector cosine).

    Args:
        query_text: Natural-language query (required).
        limit: Max number of results (1-100). Defaults to 10.
        user_id: Restrict to a specific user.
    """
    return await memory_tools.semantic_search(
        query_text=query_text, limit=limit, user_id=user_id
    )


@mcp.tool()
async def get_recent_memories(
    limit: int = 20,
    user_id: str | None = None,
) -> dict:
    """Return the most recently created memories.

    Args:
        limit: Max number of results (1-100). Defaults to 20.
        user_id: Restrict to a specific user.
    """
    return await memory_tools.get_recent_memories(limit=limit, user_id=user_id)


@mcp.tool()
async def delete_memory(memory_id: str) -> dict:
    """Delete a memory by its id.

    Args:
        memory_id: The id of the memory to delete (required).
    """
    return await memory_tools.delete_memory(memory_id=memory_id)


def main() -> None:
    """Run the MCP server over stdio."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()

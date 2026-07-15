# Phase 2: MCP Memory Server

Build a Model Context Protocol (MCP) server that wraps the HCC Memory API as MCP tools.

## Structure

```
mcp/
├── server.py       — MCP server entry point
├── memory.py      — Memory tool definitions
├── search.py      — Search tool definitions
└── __init__.py
```

## Tools to implement

1. `store_memory` — Store a memory
   Inputs: content (required), user_id, type, summary, importance, tags
   
2. `search_memories` — Search memories by keyword
   Inputs: query (required), user_id, type, limit

3. `semantic_search` — Search memories by semantic similarity
   Inputs: query_text (required), limit, user_id
   Note: Generate embedding using a simple hash-based embedder (use the existing one in gateway/core/embeddings.py)

4. `get_recent_memories` — Get recent memories
   Inputs: limit, user_id

5. `delete_memory` — Delete a memory
   Inputs: memory_id (required)

## Implementation

- Use the `mcp` Python package (pip install mcp)
- Server connects to the HCC PostgreSQL database directly (same models)
- Use stdio transport (for Claude Code integration)
- Add MCP server config for Claude Code
- Add Makefile target for MCP server

## Claude Code integration

Create `.claude/mcp.json` that points to the MCP server:
```json
{
  "mcpServers": {
    "hcc-memory": {
      "command": "uv",
      "args": ["run", "python", "-m", "mcp.server"]
    }
  }
}
```

Update pyproject.toml with mcp dependency.

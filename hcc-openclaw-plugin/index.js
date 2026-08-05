const DEFAULT_BASE_URL = "http://localhost:8000";
const DEFAULT_USER_ID = "default";
const DEFAULT_AGENT_ID = "openclaw";

function resolveConfig(api) {
  const cfg = api.pluginConfig || {};
  return {
    baseUrl: (cfg.baseUrl || process.env.HCC_BASE_URL || DEFAULT_BASE_URL).replace(/\/+$/, ""),
    userId: cfg.userId || process.env.HCC_USER_ID || DEFAULT_USER_ID,
    agentId: cfg.agentId || process.env.HCC_AGENT_ID || DEFAULT_AGENT_ID,
  };
}

async function hccFetch(baseUrl, path, { method = "GET", body } = {}) {
  const res = await fetch(`${baseUrl}/api/v1${path}`, {
    method,
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(`HCC ${method} ${path} -> ${res.status} ${text}`);
  }
  return res.json();
}

function jsonResult(value) {
  return { content: [{ type: "text", text: JSON.stringify(value, null, 2) }] };
}

async function storeToHcc(baseUrl, { userId, agentId, content, summary = "", type = "general", tags = [], source = "openclaw_plugin", importance = 0.5 }) {
  if (!content || !content.trim()) return null;
  return hccFetch(baseUrl, "/memory/store", {
    method: "POST",
    body: { user_id: userId, agent_id: agentId, shared: false, type, content, summary, importance, tags, source },
  });
}

function extractMessageText(message) {
  if (!message) return "";
  if (typeof message.content === "string") return message.content;
  if (Array.isArray(message.content)) {
    return message.content
      .map((part) => (typeof part === "string" ? part : part?.text || ""))
      .filter(Boolean)
      .join("\n");
  }
  return "";
}

const MemorySearchSchema = {
  type: "object",
  properties: {
    query: { type: "string" },
    maxResults: { type: "integer", minimum: 1 },
  },
  required: ["query"],
  additionalProperties: false,
};

const MemoryGetSchema = {
  type: "object",
  properties: {
    id: { type: "string", description: "HCC memory id to fetch exactly" },
    content: { type: "string", description: "Fallback: closest match by content/summary substring" },
  },
  additionalProperties: false,
};

// `kind: "memory"` is deliberately omitted for now — it puts the whole
// plugin behind OpenClaw's exclusive `plugins.slots.memory` gate (only the
// slot owner loads at all), which would disable memory-core. Add it back
// here (and in openclaw.plugin.json) only when actually handing off the slot.
export default {
  id: "hcc-memory",
  name: "HCC Memory",
  kind: "memory",
  description: "Bridges OpenClaw memory to HCC (Hanyan Cognitive Core) REST API",

  register(api) {
    const { baseUrl, userId, agentId } = resolveConfig(api);
    const log = api.logger || console;

    api.registerTool({
      name: "memory_search",
      description:
        "Search HCC (Hanyan Cognitive Core) long-term memory via hybrid BM25+vector search. " +
        "Use before answering questions about prior work, decisions, dates, people, preferences, or todos.",
      parameters: MemorySearchSchema,
      async execute(_toolCallId, params) {
        try {
          const data = await hccFetch(baseUrl, "/memory/hybrid-search", {
            method: "POST",
            body: {
              query: params.query,
              limit: params.maxResults || 10,
              user_id: userId,
              agent_id: agentId,
            },
          });
          const items = (data.items || []).map((it) => ({
            id: it.memory.id,
            content: it.memory.content,
            summary: it.memory.summary,
            score: it.rrf_score,
            created_at: it.memory.created_at,
          }));
          return jsonResult({ items, total: data.total ?? items.length });
        } catch (err) {
          log.error?.(`[hcc-memory] memory_search failed: ${err.message}`);
          return jsonResult({ items: [], total: 0, error: String(err.message || err) });
        }
      },
    });

    api.registerTool({
      name: "memory_get",
      description: "Fetch a specific HCC memory by id, or the closest match by content substring.",
      parameters: MemoryGetSchema,
      async execute(_toolCallId, params) {
        try {
          if (params.id) {
            const data = await hccFetch(baseUrl, "/memory/recent?limit=200", { method: "GET" });
            const found = (data.items || []).find((m) => m.id === params.id);
            return jsonResult({ found: Boolean(found), memory: found || null });
          }
          if (params.content) {
            const data = await hccFetch(baseUrl, "/memory/search", {
              method: "POST",
              body: { query: params.content, user_id: userId, agent_id: agentId, limit: 1 },
            });
            const found = data.items?.[0] || null;
            return jsonResult({ found: Boolean(found), memory: found });
          }
          return jsonResult({ found: false, error: "id or content is required" });
        } catch (err) {
          log.error?.(`[hcc-memory] memory_get failed: ${err.message}`);
          return jsonResult({ found: false, error: String(err.message || err) });
        }
      },
    });

    api.on("session_end", async (event, ctx) => {
      try {
        const content =
          `[OpenClaw session_end] session=${event.sessionId} agent=${ctx.agentId || agentId} ` +
          `messages=${event.messageCount} reason=${event.reason || "unknown"} durationMs=${event.durationMs ?? "?"}`;
        await storeToHcc(baseUrl, { userId, agentId, content, type: "session", tags: ["openclaw", "session_end"] });
      } catch (err) {
        log.error?.(`[hcc-memory] session_end store failed: ${err.message}`);
      }
    });

    api.on("before_compaction", async (event, ctx) => {
      try {
        const content =
          `[OpenClaw before_compaction] session=${ctx.sessionId || "?"} agent=${ctx.agentId || agentId} ` +
          `messageCount=${event.messageCount} compactingCount=${event.compactingCount ?? "?"} tokenCount=${event.tokenCount ?? "?"}`;
        await storeToHcc(baseUrl, { userId, agentId, content, type: "session", tags: ["openclaw", "before_compaction"] });
      } catch (err) {
        log.error?.(`[hcc-memory] before_compaction store failed: ${err.message}`);
      }
    });

    api.on("tool_result_persist", (event, ctx) => {
      const toolName = event.toolName || ctx.toolName || "unknown_tool";
      const text = extractMessageText(event.message);
      if (!text) return;
      storeToHcc(baseUrl, {
        userId,
        agentId,
        content: `[OpenClaw tool_result:${toolName}] ${text}`.slice(0, 4000),
        type: "tool_result",
        tags: ["openclaw", "tool_result", toolName],
        importance: 0.3,
      }).catch((err) => {
        log.error?.(`[hcc-memory] tool_result_persist store failed: ${err.message}`);
      });
    });

    // Guarded: without `kind: "memory"` declared (see note above), this call
    // is expected to no-op or throw until the slot is handed off — must not
    // take down memory_search/memory_get/hook registration above if it does.
    try {
      api.registerMemoryCapability({
      runtime: {
        async getMemorySearchManager({ agentId: reqAgentId } = {}) {
          const scopedAgentId = reqAgentId || agentId;
          return {
            manager: {
              async search(query, opts = {}) {
                const data = await hccFetch(baseUrl, "/memory/hybrid-search", {
                  method: "POST",
                  body: { query, limit: opts.maxResults || 10, user_id: userId, agent_id: scopedAgentId },
                });
                return (data.items || []).map((it) => ({
                  path: it.memory.id,
                  startLine: 1,
                  endLine: 1,
                  score: it.rrf_score,
                  snippet: it.memory.summary || (it.memory.content || "").slice(0, 280),
                  source: "memory",
                  citation: it.memory.id,
                }));
              },
              async readFile({ relPath }) {
                const data = await hccFetch(baseUrl, "/memory/recent?limit=200", { method: "GET" });
                const found = (data.items || []).find((m) => m.id === relPath);
                return { text: found ? found.content : "", path: relPath, truncated: false };
              },
              status() {
                return { backend: "builtin", provider: "hcc-memory", workspaceDir: baseUrl };
              },
              async probeEmbeddingAvailability() {
                return { ok: true, checked: true };
              },
              async probeVectorAvailability() {
                return true;
              },
            },
          };
        },
        resolveMemoryBackendConfig() {
          return { provider: "hcc-memory", baseUrl };
        },
      },
      });
    } catch (err) {
      log.error?.(`[hcc-memory] registerMemoryCapability skipped: ${err.message}`);
    }
  },
};

const DEFAULT_BASE_URL = "http://localhost:8000";
const DEFAULT_USER_ID = "default";
const DEFAULT_AGENT_ID = "openclaw";
const DEFAULT_SESSION_RECALL_LIMIT = 5;
const DEFAULT_SESSION_RECALL_FETCH = 20; // pool fetched before ranking by importance, see buildSessionContext

function resolveConfig(api) {
  const cfg = api.pluginConfig || {};
  const truthyEnv = (v) => v === "1" || v === "true";
  return {
    baseUrl: (cfg.baseUrl || process.env.HCC_BASE_URL || DEFAULT_BASE_URL).replace(/\/+$/, ""),
    userId: cfg.userId || process.env.HCC_USER_ID || DEFAULT_USER_ID,
    agentId: cfg.agentId || process.env.HCC_AGENT_ID || DEFAULT_AGENT_ID,
    sessionRecallEnabled: cfg.sessionRecallEnabled ?? !truthyEnv(process.env.HCC_SESSION_RECALL_DISABLED) ?? true,
    sessionRecallLimit: cfg.sessionRecallLimit || Number(process.env.HCC_SESSION_RECALL_LIMIT) || DEFAULT_SESSION_RECALL_LIMIT,
    emotionEnabled: cfg.emotionEnabled ?? !truthyEnv(process.env.HCC_EMOTION_DISABLED) ?? true,
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

// --- session_start auto-recall (体检报告 P0-2) + emotion warm-start (P1-4) ---
//
// OpenClaw's session_start hook fires at session boundaries but is
// observation-only — it cannot inject content into the prompt (see
// docs.openclaw.ai/plugins/hooks). So the actual injection happens in
// before_prompt_build, which *can* return prependSystemContext/
// appendSystemContext. We bridge the two: session_start does the HCC
// fetch (recall + emotion) and stashes the rendered text keyed by session
// id; the *first* before_prompt_build call for that session consumes and
// clears it, so it's injected once at session start, not re-injected on
// every turn.
const pendingSessionContext = new Map(); // sessionId -> { text, memoryIds }

function sessionIdOf(event, ctx) {
  return event?.sessionId || ctx?.sessionId || ctx?.agentId || null;
}

function renderSessionContext(memories, emotion) {
  const lines = [];
  if (memories.length) {
    lines.push("[HCC 记忆回顾 / auto-recalled memories]");
    for (const m of memories) {
      const headline = (m.summary || m.content || "").split("\n")[0].slice(0, 160);
      lines.push(`- ${headline}`);
    }
  }
  if (emotion) {
    lines.push(
      `[HCC 情绪状态 / emotion] ${emotion.named_state || emotion.primary_emotion || "?"}` +
        (emotion.expression_hint ? ` — ${emotion.expression_hint}` : "")
    );
  }
  return lines.join("\n");
}

async function buildSessionContext(baseUrl, { userId, agentId, sessionRecallEnabled, sessionRecallLimit, emotionEnabled }, log) {
  let memories = [];
  let emotion = null;

  if (sessionRecallEnabled) {
    try {
      // Empty query + user/agent scope = "recent for this scope" (MemoryService.search
      // skips the ILIKE filter when query is blank, still filters/orders normally) — see
      // gateway/services/__init__.py. Over-fetch a pool, then rank by importance so the
      // top-N "relevant" memories aren't just the most recent chatter.
      const data = await hccFetch(baseUrl, "/memory/search", {
        method: "POST",
        body: { query: "", user_id: userId, agent_id: agentId, limit: DEFAULT_SESSION_RECALL_FETCH },
      });
      memories = (data.items || [])
        .slice()
        .sort((a, b) => (b.importance || 0) - (a.importance || 0))
        .slice(0, sessionRecallLimit);
    } catch (err) {
      log.error?.(`[hcc-memory] session_start recall failed: ${err.message}`);
    }
  }

  if (emotionEnabled) {
    try {
      emotion = await hccFetch(baseUrl, "/emotion/state", { method: "GET" });
    } catch (err) {
      log.error?.(`[hcc-memory] session_start emotion fetch failed: ${err.message}`);
    }
  }

  const text = renderSessionContext(memories, emotion);
  return { text, memoryIds: memories.map((m) => m.id) };
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
    const { baseUrl, userId, agentId, sessionRecallEnabled, sessionRecallLimit, emotionEnabled } = resolveConfig(api);
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

    // 体检报告 P0-2: session_start can't inject content itself (observation-only,
    // see comment above buildSessionContext), so it fetches + stashes; the next
    // before_prompt_build call for this session consumes the stash and injects it.
    api.on("session_start", async (event, ctx) => {
      const sid = sessionIdOf(event, ctx);
      if (!sid) return;
      // Only warm-start on session boundaries that actually begin a fresh
      // context — a compaction/idle/shutdown/restart tick isn't "starting to
      // talk to someone new", so re-injecting recall there would just be noise
      // (or, for compaction, actively wrong — the whole point of compaction is
      // the model already has the context, condensed).
      if (!["new", "reset", "daily"].includes(event?.reason)) return;
      try {
        const built = await buildSessionContext(baseUrl, { userId, agentId, sessionRecallEnabled, sessionRecallLimit, emotionEnabled }, log);
        if (built.text) {
          pendingSessionContext.set(sid, built);
          log.info?.(
            `[hcc-memory] session_start recall: session=${sid} reason=${event.reason} ` +
              `memories=${built.memoryIds.length} emotion=${emotionEnabled}`
          );
        }
        // Recall counts as "recalled" for the forget engine's access_count/last_access
        // signal (see gateway/api/memory_routes.py touch_memories) — fire-and-forget,
        // must not delay session start on HCC being slow/down.
        if (built.memoryIds.length) {
          hccFetch(baseUrl, "/memory/touch", { method: "POST", body: { ids: built.memoryIds } }).catch((err) => {
            log.error?.(`[hcc-memory] session_start touch failed: ${err.message}`);
          });
        }
      } catch (err) {
        log.error?.(`[hcc-memory] session_start recall failed: ${err.message}`);
      }
    });

    api.on("before_prompt_build", async (_event, ctx) => {
      const sid = sessionIdOf(_event, ctx);
      if (!sid) return;
      const pending = pendingSessionContext.get(sid);
      if (!pending) return;
      pendingSessionContext.delete(sid); // inject once per session, not every turn
      return { prependSystemContext: pending.text };
    });

    api.on("session_end", async (event, ctx) => {
      const content =
        `[OpenClaw session_end] session=${event.sessionId} agent=${ctx.agentId || agentId} ` +
        `messages=${event.messageCount} reason=${event.reason || "unknown"} durationMs=${event.durationMs ?? "?"}`;
      try {
        await storeToHcc(baseUrl, { userId, agentId, content, type: "session", tags: ["openclaw", "session_end"] });
      } catch (err) {
        log.error?.(`[hcc-memory] session_end store failed: ${err.message}`);
      }
      // 体检报告 P1-4: write the emotion state back at session end, same text the
      // session summary was built from — update_and_persist folds it into the
      // 6-dim state via keyword/sentiment triggers (core/emotion.py), so the
      // next session_start's GET /emotion/state reflects what happened here.
      if (emotionEnabled) {
        try {
          await hccFetch(baseUrl, "/emotion/update", {
            method: "POST",
            body: { text: content, source: "openclaw_session_end" },
          });
        } catch (err) {
          log.error?.(`[hcc-memory] session_end emotion update failed: ${err.message}`);
        }
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

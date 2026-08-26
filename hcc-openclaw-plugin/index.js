import { readFile, stat } from "node:fs/promises";
import os from "node:os";
import path from "node:path";

const DEFAULT_BASE_URL = "http://100.66.103.69:8000";
const SOUL_BASE_URL = "http://100.66.103.69:8732"; // soul 实时情绪(方案A): Mac MLX 17维
const DEFAULT_USER_ID = "michael";
const DEFAULT_AGENT_ID = "openclaw";
const DEFAULT_SESSION_RECALL_LIMIT = 5;
const DEFAULT_SESSION_RECALL_FETCH = 20; // pool fetched before ranking by importance, see buildSessionContext
const DEFAULT_FETCH_TIMEOUT_MS = 8000; // abort a hung HCC request before it stalls prompt building
const CACHE_MAX_SESSIONS = 100; // FIFO cap for both pendingSessionContext and turnContextCache

// P3-2: shared with sse_monitor.py (same default dir) — the monitor touches
// cache_invalidate.marker's mtime on every memory.created/updated/deleted
// event; before_prompt_build's turnContextCache read below compares its own
// cachedAt against this mtime to force an early refresh instead of waiting
// out the turn-throttle when memory actually changed underneath it.
const HCC_EVENTS_DIR =
  process.env.HCC_EVENTS_DIR || path.join(os.homedir(), ".openclaw/workspace/memory/hcc-events");
const CACHE_INVALIDATE_MARKER = path.join(HCC_EVENTS_DIR, "cache_invalidate.marker");

// Effective timeout, overridable via config/env (see resolveConfig). hccFetch
// falls back to this when a call doesn't pass timeoutMs explicitly.
let activeFetchTimeoutMs = DEFAULT_FETCH_TIMEOUT_MS;

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
    fetchTimeoutMs: cfg.fetchTimeoutMs || Number(process.env.HCC_FETCH_TIMEOUT_MS) || DEFAULT_FETCH_TIMEOUT_MS,
    // When on (default), *active* memory reads (memory_search / memory_get /
    // capability search) drop the agent filter so openclaw can retrieve
    // memories written by hermes / claude-code(hanyan) too — honouring 公子's
    // "three runtimes share one memory" rule. Writes and emotion stay scoped to
    // this agent. Set false (or HCC_CROSS_AGENT_SEARCH_DISABLED=1) to box search
    // back to this agent's own memories.
    crossAgentSearch: cfg.crossAgentSearch ?? !truthyEnv(process.env.HCC_CROSS_AGENT_SEARCH_DISABLED),
  };
}

async function hccFetch(baseUrl, path, { method = "GET", body, timeoutMs, raw = false } = {}) {
  // raw=true: 不加 /api/v1 前缀(给 soul 等非 HCC-gateway 服务用)
  const url = raw ? `${baseUrl}${path}` : `${baseUrl}/api/v1${path}`;
  const res = await fetch(url, {
    method,
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
    signal: AbortSignal.timeout(timeoutMs || activeFetchTimeoutMs),
  });
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(`HCC ${method} ${path} -> ${res.status} ${text}`);
  }
  return res.json();
}

// FIFO eviction helper for the two session-keyed caches below — a gateway
// process can accumulate hundreds of session ids over weeks of uptime; an
// unbounded Map would leak. Cap at CACHE_MAX_SESSIONS, evict oldest first.
function cacheSet(cache, sid, value) {
  cache.set(sid, value);
  if (cache.size > CACHE_MAX_SESSIONS) {
    const oldest = cache.keys().next().value;
    if (oldest !== undefined) cache.delete(oldest);
  }
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

// --- P3-1: session_end / before_compaction conversation persistence ---
//
// Before this, session_end/before_compaction only wrote a one-line metadata
// string ("session=x messages=N") — none of the actual conversation ever
// reached HCC, so it couldn't become shared cross-runtime memory. Neither
// hook's event carries a plain `messages` array reliably though: session_end
// only ever gives a `sessionFile` (JSONL transcript path) to read from disk;
// before_compaction sometimes carries `event.messages` directly (in-memory
// harness path) and sometimes only `sessionFile` or neither (see OpenClaw's
// several before_compaction call sites — messageCount-only variants exist).
// resolveConversationMessages() below tries the in-memory array first, falls
// back to reading the transcript file, and returns [] if neither is present
// (metadata-only write still happens either way).
const CONVERSATION_EXCERPT_MAX_CHARS = 2000;
const CONVERSATION_SAMPLE_CAP = 12; // head+mid+tail sample size before truncation

function messagesFromEventArray(rawMessages) {
  if (!Array.isArray(rawMessages)) return [];
  return rawMessages
    .map((m) => ({ role: m?.role, text: extractMessageText(m).trim() }))
    .filter((m) => (m.role === "user" || m.role === "assistant") && m.text);
}

async function messagesFromSessionFile(sessionFile) {
  if (!sessionFile) return [];
  let raw;
  try {
    raw = await readFile(sessionFile, "utf-8");
  } catch {
    return []; // file gone / archived elsewhere / not readable — degrade to metadata-only
  }
  const messages = [];
  for (const line of raw.split("\n")) {
    if (!line.trim()) continue;
    let obj;
    try {
      obj = JSON.parse(line);
    } catch {
      continue;
    }
    if (obj?.type !== "message") continue;
    const role = obj.message?.role;
    if (role !== "user" && role !== "assistant") continue;
    const text = extractMessageText(obj.message).trim();
    if (text) messages.push({ role, text });
  }
  return messages;
}

async function resolveConversationMessages(event) {
  const fromEvent = messagesFromEventArray(event?.messages);
  if (fromEvent.length) return fromEvent;
  return messagesFromSessionFile(event?.sessionFile);
}

// 去重:相邻重复消息(如心跳轮询反复产生的相同文本)只保留一条。
function dedupeConsecutive(messages) {
  const out = [];
  for (const m of messages) {
    const prev = out[out.length - 1];
    if (prev && prev.role === m.role && prev.text === m.text) continue;
    out.push(m);
  }
  return out;
}

// 取首尾 + 中间抽样,而不是简单截断——避免一段长会话只留下开头或结尾。
function sampleMessages(messages) {
  if (messages.length <= CONVERSATION_SAMPLE_CAP) return messages;
  const head = messages.slice(0, 2);
  const midIdx = Math.floor(messages.length / 2);
  const mid = messages.slice(Math.max(midIdx - 1, 2), midIdx + 1);
  const tailCount = Math.max(CONVERSATION_SAMPLE_CAP - head.length - mid.length, 0);
  const tail = tailCount ? messages.slice(-tailCount) : [];
  return [...head, ...mid, ...tail];
}

function renderConversationExcerpt(messages) {
  const sampled = sampleMessages(dedupeConsecutive(messages));
  const lines = sampled.map((m) => `${m.role}: ${m.text.replace(/\s+/g, " ").slice(0, 400)}`);
  const text = lines.join("\n");
  // 内容过长时保留结尾(最近发生的内容),而不是开头。
  return text.length > CONVERSATION_EXCERPT_MAX_CHARS ? text.slice(-CONVERSATION_EXCERPT_MAX_CHARS) : text;
}

// 幂等:marker 里带 session_id + 该次快照的消息数,同一状态重复触发(如
// before_compaction 在几乎无新消息时又跑一次)会命中已存的 marker 而跳过;
// 会话真正推进(消息数变化)才会产生新的快照,而不是被 session_id 单独锁死。
async function conversationAlreadyStored(baseUrl, { userId, agentId }, marker) {
  try {
    const data = await hccFetch(baseUrl, "/memory/search", {
      method: "POST",
      body: { query: marker, user_id: userId, agent_id: agentId, type: "conversation", limit: 1 },
    });
    return (data.items || []).length > 0;
  } catch {
    return false; // 探测失败按"未写入"处理——宁可偶尔重复,不可漏记
  }
}

async function storeConversationSnapshot(baseUrl, { userId, agentId }, { sessionId, reason, event }, log) {
  if (!sessionId) return;
  const messages = await resolveConversationMessages(event);
  if (!messages.length) return;
  const marker = `sid:${sessionId}:mc:${messages.length}`;
  if (await conversationAlreadyStored(baseUrl, { userId, agentId }, marker)) return;
  const excerpt = renderConversationExcerpt(messages);
  if (!excerpt) return;
  const dateStr = new Date().toISOString().slice(0, 10);
  const content = `[OpenClaw ${reason}] session=${sessionId}\n${excerpt}\n[marker:${marker}]`;
  try {
    await storeToHcc(baseUrl, {
      userId,
      agentId,
      content,
      summary: excerpt.slice(0, 200),
      type: "conversation",
      source: "openclaw_plugin",
      tags: ["openclaw", "conversation", dateStr],
    });
  } catch (err) {
    log.error?.(`[hcc-memory] ${reason} conversation store failed: ${err.message}`);
  }
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

// --- before_prompt_build turn-tail HCC context injection (方案A OpenClaw 版) ---
//
// 与上面 session_start → before_prompt_build 的一次性 prependSystemContext 注入
// (体检报告 P0-2)是两回事、互不干扰:那个只在会话第一轮跑一次,写进 system
// prompt 前缀;这里是"每轮"都可能刷新的记忆块,写进 appendContext——OpenClaw
// 把 appendContext 拼进当轮 user 消息尾部(见 dist/prepare.runtime-*.js:
// `preparedPrompt = preparedPrompt + "\n\n" + hookResult.appendContext`),不
// 进 system prompt,所以不会破坏 DeepSeek 的 system-prompt 前缀缓存。OpenClaw
// 对同一 hook 名的多个监听器按注册顺序依次跑并合并结果(mergeBeforePromptBuild,
// 见 dist/hook-runner-global-*.js),appendContext/prependSystemContext 分别
// 拼接,互不覆盖——所以这里独立注册第二个 before_prompt_build 监听器,不用改
// 上面那个。
//
// 节流参考 Hermes 插件 ~/.hermes/plugins/hcc/__init__.py 的
// _PREFETCH_MIN_INTERVAL_TURNS=3:3 轮内命中缓存不重新拉取(省一次 HCC 往返,
// 不是为了保前缀缓存——appendContext 本来就拼在"当轮"这个从不被缓存的新内容
// 里,拉不拉新都不影响历史轮次的前缀)。
const APPEND_CONTEXT_MAX_CHARS = 1500;
const APPEND_CONTEXT_THROTTLE_TURNS = 3;
const turnContextCache = new Map(); // sessionId -> { text, turnsSinceRefresh, cachedAt }

// P3-2: sse_monitor.py touches CACHE_INVALIDATE_MARKER's mtime whenever a
// memory.created/updated/deleted event arrives. If that mtime is newer than
// this cache entry's cachedAt, memory changed underneath it — force a refresh
// now instead of waiting out APPEND_CONTEXT_THROTTLE_TURNS. One stat() call,
// fails open (treat as "not stale") so a missing/unreadable marker file never
// blocks the turn.
async function cacheStaleByInvalidationMarker(cachedAt) {
  try {
    const st = await stat(CACHE_INVALIDATE_MARKER);
    return st.mtimeMs > cachedAt;
  } catch {
    return false;
  }
}

function lastUserMessageText(messages) {
  if (!Array.isArray(messages)) return "";
  for (let i = messages.length - 1; i >= 0; i--) {
    if (messages[i]?.role === "user") return extractMessageText(messages[i]);
  }
  return "";
}

async function fetchTurnContextBlock(baseUrl, { userId, agentId }, query, log) {
  try {
    const data = await hccFetch(baseUrl, "/context", {
      method: "POST",
      body: { query, user_id: userId, agent_id: agentId, include_emotion: true },
    });
    let text = String(data?.context || "").trim();
    // 方案A: soul 实时情绪直读——对"当前这句用户消息"调 soul 编码器拿 17 维
    // 情绪, 拼进 appendContext 尾部。失败静默(不影响记忆块), 不阻塞对话。
    if (query) {
      try {
        const soul = await hccFetch(SOUL_BASE_URL, "/soul/encode", {
          method: "POST",
          body: { text: query },
          timeoutMs: 3000,
          raw: true, // soul 无 /api/v1 前缀
        });
        const dims = soul?.dims || {};
        const named = soul?.named_state || null;
        const top = Object.entries(dims)
          .filter(([, v]) => Math.abs(v) > 0.05)
          .sort((a, b) => Math.abs(b[1]) - Math.abs(a[1]))
          .slice(0, 4);
        const parts = [];
        let nsName = null, nsProb = null;
        if (named && Array.isArray(named) && named.length) {
          nsName = named[0].state || named[0].name || null;
          nsProb = named[0].prob;
        } else if (named && typeof named === "object") {
          nsName = named.state || named.name || (typeof named === "string" ? named : null);
          nsProb = named.prob;
        } else if (typeof named === "string") {
          nsName = named;
        }
        if (nsName) {
          parts.push('命名态:' + nsName + (nsProb != null ? '(概率' + Number(nsProb).toFixed(2) + ')' : ''));
        }
        if (top.length) {
          parts.push(top.map(([k, v]) => k + '=' + (v > 0 ? "+" : "") + v.toFixed(2)).join(" "));
        }
        if (parts.length) {
          text += (text ? "\n" : "") + "[soul 实时情绪] " + parts.join(" | ");
        }
      } catch (err) {
        log.error?.(`[hcc-memory] soul encode failed: ${err.message}`);
      }
    }
    return text.length > APPEND_CONTEXT_MAX_CHARS ? text.slice(0, APPEND_CONTEXT_MAX_CHARS) : text;
  } catch (err) {
    log.error?.(`[hcc-memory] before_prompt_build turn context fetch failed: ${err.message}`);
    return undefined; // undefined = 请求失败,区分于请求成功但检索为空的 ""
  }
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
      emotion = await hccFetch(
        baseUrl,
        `/emotion/state?user_id=${encodeURIComponent(userId)}&agent_id=${encodeURIComponent(agentId)}`,
        { method: "GET" }
      );
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

// `kind: "memory"` is declared here AND in openclaw.plugin.json, and the
// production config assigns the memory slot to this plugin
// (`plugins.slots.memory: "hcc-memory"`, memory-core disabled). The slot is
// deliberately owned: hcc-memory is the memory backend for OpenClaw on n100.
// If you ever want to coexist with memory-core instead, remove `kind` from
// both files and drop the slot assignment, then registerMemoryCapability
// below will no-op (it's already guarded).
export default {
  id: "hcc-memory",
  name: "HCC Memory",
  kind: "memory",
  description: "Bridges OpenClaw memory to HCC (Hanyan Cognitive Core) REST API",

  register(api) {
    const { baseUrl, userId, agentId, sessionRecallEnabled, sessionRecallLimit, emotionEnabled, fetchTimeoutMs, crossAgentSearch } = resolveConfig(api);
    activeFetchTimeoutMs = fetchTimeoutMs;
    // null agent_id = no agent filter server-side (gateway hybrid_search/search
    // skip the filter when agent_id is null — see P1-3). Used for active reads
    // only; writes/emotion keep `agentId`.
    const searchAgentId = crossAgentSearch ? null : agentId;
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
              agent_id: searchAgentId,
            },
          });
          const items = (data.items || []).map((it) => ({
            id: it.memory.id,
            content: it.memory.content,
            summary: it.memory.summary,
            source: it.memory.source,
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
            // HCC has no GET /memory/{id} and /memory/search's limit caps at
            // 100 (MemorySearch.le=100), so walk pages of the scoped,
            // blank-query search (user/agent filter + created_at desc — ILIKE
            // is skipped when query is blank) up to a 1000-item window. Don't
            // use /memory/recent: it's unscoped global recency and may not
            // contain this agent's memories at all.
            let found = null;
            for (let offset = 0; offset < 1000 && !found; offset += 100) {
              const data = await hccFetch(baseUrl, "/memory/search", {
                method: "POST",
                body: { query: "", user_id: userId, agent_id: searchAgentId, limit: 100, offset },
              });
              const items = data.items || [];
              found = items.find((m) => m.id === params.id) || null;
              if (items.length < 100) break; // no more pages
            }
            if (found) return jsonResult({ found: true, memory: found });
            // id not in the scoped window: try content search as a last resort
            const searchData = await hccFetch(baseUrl, "/memory/search", {
              method: "POST",
              body: { query: params.id, user_id: userId, agent_id: searchAgentId, limit: 1 },
            });
            const byId = searchData.items?.[0];
            return jsonResult({ found: Boolean(byId && byId.id === params.id), memory: byId && byId.id === params.id ? byId : null });
          }
          if (params.content) {
            const data = await hccFetch(baseUrl, "/memory/search", {
              method: "POST",
              body: { query: params.content, user_id: userId, agent_id: searchAgentId, limit: 1 },
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
      // NOTE (2026-08-06): OpenClaw's session_start payload has NO `reason`
      // field — only sessionId/sessionKey/resumedFrom (verified in
      // dist/active-sessions-shutdown-tracker-*.js). The old gate
      // `if (!["new","reset","daily"].includes(event?.reason)) return;`
      // therefore never matched and silently disabled session recall.
      // OpenClaw only fires session_start when isNewSession is true, which is
      // already the correct "fresh context" boundary; resumedFrom is kept as a
      // defensive no-op guard for the same-session edge case.
      if (event?.resumedFrom && event?.resumedFrom === event?.sessionId) return;
      try {
        const built = await buildSessionContext(baseUrl, { userId, agentId, sessionRecallEnabled, sessionRecallLimit, emotionEnabled }, log);
        if (built.text) {
          cacheSet(pendingSessionContext, sid, built);
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

    // 任务2:每轮 HCC 记忆注入 appendContext(尾部,不进 system prompt)。见上面
    // turnContextCache 一段的注释。
    api.on("before_prompt_build", async (event, ctx) => {
      const sid = sessionIdOf(event, ctx);
      if (!sid) return;
      const query = lastUserMessageText(event?.messages);
      if (!query) return;

      let entry = turnContextCache.get(sid);
      const shouldRefresh =
        !entry ||
        entry.turnsSinceRefresh >= APPEND_CONTEXT_THROTTLE_TURNS ||
        (await cacheStaleByInvalidationMarker(entry.cachedAt));
      if (shouldRefresh) {
        const block = await fetchTurnContextBlock(baseUrl, { userId, agentId }, query, log);
        if (block === undefined) {
          // HCC 请求失败:保留旧块(若有),节流计数不清零,下一轮立刻重试
          // (参考 Hermes _run_prefetch 失败分支不更新 _last_refresh_turn 的做法)。
        } else if (block) {
          entry = { text: block, turnsSinceRefresh: 0, cachedAt: Date.now() };
          cacheSet(turnContextCache, sid, entry);
        } else {
          // 请求成功但没检索到相关记忆:不是故障,清掉旧块——继续注入一条与
          // 当前问题无关的陈旧记忆,比不注入更容易误导对话。
          turnContextCache.delete(sid);
          entry = undefined;
        }
      } else {
        entry.turnsSinceRefresh += 1;
      }
      return entry?.text ? { appendContext: entry.text } : undefined;
    });

    api.on("session_end", async (event, ctx) => {
      const sid = sessionIdOf(event, ctx);
      // Drop per-session caches for this session — no point keeping a recalled
      // context block or turn-tail block for a session that just ended.
      if (sid) {
        pendingSessionContext.delete(sid);
        turnContextCache.delete(sid);
      }
      const content =
        `[OpenClaw session_end] session=${event.sessionId} agent=${ctx.agentId || agentId} ` +
        `messages=${event.messageCount} reason=${event.reason || "unknown"} durationMs=${event.durationMs ?? "?"}`;
      try {
        await storeToHcc(baseUrl, { userId, agentId, content, type: "session", tags: ["openclaw", "session_end"] });
      } catch (err) {
        log.error?.(`[hcc-memory] session_end store failed: ${err.message}`);
      }
      // P3-1: metadata write above stays as-is; this additionally persists the
      // actual conversation content so it becomes shared cross-runtime memory.
      await storeConversationSnapshot(
        baseUrl,
        { userId, agentId: ctx.agentId || agentId },
        { sessionId: event.sessionId, reason: "session_end", event },
        log
      );
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
      // P3-1: same conversation persistence as session_end — before_compaction
      // fires repeatedly over a long session, so this is often the ONLY chance
      // to capture content that gets summarized away by compaction.
      await storeConversationSnapshot(
        baseUrl,
        { userId, agentId: ctx.agentId || agentId },
        { sessionId: ctx.sessionId || event.sessionId, reason: "before_compaction", event },
        log
      );
    });

    // Tools whose output must NEVER be persisted back into HCC. Memory/recall
    // tools are self-referential — persisting a memory_search result stores a
    // memory whose content is a quoted list of other memories, which then gets
    // recalled and re-quoted next turn (a tool-log-of-a-tool-log spiral). The
    // HCC-side context builder now also drops type=tool_result rows entirely, so
    // these logs no longer reach prompt injection regardless; this denylist
    // stops the DB bloat and the recursive-quoting at the source.
    const TOOL_RESULT_PERSIST_DENYLIST = new Set([
      "memory_search",
      "memory_get",
      "memory_list",
      "search_memories",
      "semantic_search",
      "hybrid_search",
      "recall",
      "get_recent_memories",
      "context",
    ]);

    // Content-based guard (2026-08-26): the denylist above stops the memory_*
    // TOOLS, but openclaw also searches memory via `exec` (a shell script /
    // curl that dumps HCC results), and exec output IS persisted. Those dumps
    // quote other memories ("匹配度 N" / re-nested "[OpenClaw tool_result:…]" /
    // "source=openclaw_sync"), reigniting the exact recursive-quoting spiral —
    // just through a tool the name-denylist can't see. This signature check
    // drops any tool_result whose CONTENT is a memory-search dump, regardless
    // of which tool produced it, killing the recursion at the true source.
    const MEMORY_DUMP_RE =
      /匹配度\s*\d|── 匹配度|\[OpenClaw tool_result:|source=(?:openclaw_sync|openclaw_plugin|mcp|hermes)\b/;

    api.on("tool_result_persist", (event, ctx) => {
      const toolName = event.toolName || ctx.toolName || "unknown_tool";
      if (TOOL_RESULT_PERSIST_DENYLIST.has(toolName)) return;
      const text = extractMessageText(event.message);
      if (!text) return;
      if (MEMORY_DUMP_RE.test(text)) return;   // 记忆搜索转储 → 不入库(杜绝递归)
      storeToHcc(baseUrl, {
        userId,
        agentId,
        content: `[OpenClaw tool_result:${toolName}] ${text}`.slice(0, 4000),
        type: "tool_result",
        // Below the gateway's noise-filter threshold (0.5) so these never
        // surface in search/context. Keep it here and do NOT raise it — the
        // 252 legacy rows that leaked into context were ones written at >=0.5.
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
          // Explicit caller-supplied agent wins; otherwise fall back to the
          // cross-agent read scope (null when crossAgentSearch is on).
          const scopedAgentId = reqAgentId || searchAgentId;
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
                // Scoped blank-query search with pagination (search limit caps
                // at 100 — see memory_get note) instead of unscoped
                // /memory/recent.
                let found = null;
                for (let offset = 0; offset < 1000 && !found; offset += 100) {
                  const data = await hccFetch(baseUrl, "/memory/search", {
                    method: "POST",
                    body: { query: "", user_id: userId, agent_id: scopedAgentId, limit: 100, offset },
                  });
                  const items = data.items || [];
                  found = items.find((m) => m.id === relPath) || null;
                  if (items.length < 100) break;
                }
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

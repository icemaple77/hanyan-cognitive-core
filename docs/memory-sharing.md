# 三方共享记忆 — agent_id 规范与事件同步

三个运行时（OpenClaw/含烟、Claude Code、Hermes）共享同一个 HCC 实例
（gateway `:8000` + 一份 PostgreSQL/pgvector），靠 `agent_id` 区分写入来源，
靠跨 scope 检索让彼此可见。本文档是这套约定的唯一权威说明——改
`agent_id` 默认值或检索的 scope 规则时，先改这里。

## agent_id 规范

| 运行时 | agent_id | 谁设置的默认值 |
|---|---|---|
| OpenClaw（含烟主实例） | `openclaw` | `hcc-openclaw-plugin/index.js` `resolveConfig()`，`DEFAULT_AGENT_ID` |
| Claude Code | `claude-code` | `~/.claude.json` → `mcpServers.hcc.env.HCC_AGENT_ID`，经 `mcp/server.py` 的 `_DEFAULT_AGENT_ID = os.environ.get("HCC_AGENT_ID", "default")` |
| Hermes | `hermes` | `~/.hermes/plugins/hcc/__init__.py`，`self._agent_id`（`HCC_AGENT_ID` 环境变量 > 插件配置 `agent_id` > 硬编码 `"hermes"`） |

user_id 统一用 `michael`（公子），三方一致，不做区分。

**写入永远带 agent_id**（`/memory/store` 的 `agent_id` 字段），**读取默认跨
agent**：

- OpenClaw 的 `memory_search`/`memory_get`/`registerMemoryCapability` 在
  `crossAgentSearch`（默认 true）下把 `agent_id` 过滤置空，能读到 hermes /
  claude-code 写的记忆。
- Hermes 的 `search`/`context` 走同样的思路（见
  `~/.hermes/plugins/hcc/__init__.py` 顶部注释）。
- Claude Code 经 MCP 的 `search_memories`/`recall`/`hybrid_search` 等工具
  `agent_id` 参数默认 `None`（不过滤），同样是跨 agent 读。
- gateway 侧：`agent_id=None` 时 `MemoryService.search`/`hybrid_search` 跳过
  agent_id 过滤（见 `gateway/services/__init__.py`），这是"跨 agent 可见"的
  唯一实现点——三个客户端的跨 agent 行为都靠**不传 agent_id**，不是靠 gateway
  另开一个"共享池"。

新增运行时接入 HCC 时：写入必须带一个新的 agent_id 值（不要复用别人的），
读取默认不加 agent_id 过滤（除非有意只看自己的记忆）。

## 事件流如何同步（P3-2）

```
memory_routes.py (store/update/delete)
  → publish_memory_event()  (gateway/core/events.py)
    → EventBus  (进程内直连；HCC_REDIS_ENABLED=true 时经 Redis pub/sub 跨进程)
      → GET /api/v1/events/stream  (gateway/api/events_routes.py, SSE)
        → sse_monitor.py（OpenClaw/N100 常驻进程）
          → hcc-events.log          (全量事件日志，人工排查用)
          → memory_changes.jsonl    (仅 memory.* 事件，供本地工具/未来钩子读取，2000 行滚动裁剪)
          → cache_invalidate.marker (mtime 信号)
            → hcc-openclaw-plugin/index.js 的 turnContextCache 每轮 stat()
              这个文件；比缓存写入时间新，就提前刷新，不等 3 轮节流窗口
```

`_format_sse` 之前只透出 `action`/`memory_id`/`timestamp`/`source`，
`publish_memory_event` 传的 `user_id`/`agent_id`/`type`/`tags`/`importance`
其实已经在 `event.payload` 里，只是没被序列化出来——这是 P3-2 顺手修的一个
真实 bug，不是新增字段。

## 对话内容写入（P3-1）

OpenClaw 的 `session_end`/`before_compaction` 钩子过去只写一行元数据
（"session=x messages=N"），对话本身从不进 HCC。现在两个钩子额外调用
`storeConversationSnapshot()`：

- `session_end` 的 event 只带 `sessionFile`（JSONL 会话记录路径），从磁盘读；
  `before_compaction` 有时带 `event.messages`（内存中的消息数组，见 OpenClaw
  `selection-*.js`/`agent-harness-runtime-*.js` 的调用点），优先用这个，没有
  再退回读 `sessionFile`。
- 消息数超过 12 条时取首尾各几条 + 中间抽样，而不是简单截断，最终内容截到
  2000 字符（保留最近的部分）。
- 幂等 key 是 `sid:<sessionId>:mc:<消息条数>`——同一状态重复触发（如
  `before_compaction` 在几乎没有新消息时又跑一次）会命中已有记录而跳过；
  会话真正推进（消息数变化）才产生新快照。写入前用 `/memory/search` 按这个
  marker 查一次（marker 落在 content 里，ILIKE 能命中，HCC 没有单独的 tag
  过滤接口）。
- `type=conversation`，`source=openclaw_plugin`，
  `tags=["openclaw","conversation",YYYY-MM-DD]`。

## 排查清单

- 一条记忆该被别的 agent 看到却搜不到：先查调用方有没有传非空 `agent_id`
  （多数客户端默认是 `None`/跨 agent，但显式传了具体值就会被过滤）。
- OpenClaw 的 turnContextCache 感觉"过期"（记忆改了但没体现在下一轮）：查
  `~/.openclaw/workspace/memory/hcc-events/cache_invalidate.marker` 的 mtime
  是否比预期新；如果 sse_monitor 没在跑，这个信号也不会到。
- Claude Code 写入的记忆 agent_id 不是 `claude-code`：检查
  `~/.claude.json` 的 `mcpServers.hcc.env.HCC_AGENT_ID` 是否还在，改完这个
  值要重启 Claude Code 让 MCP server 子进程重新拉起才生效。

# hcc-memory — HCC × OpenClaw 插件

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
![Works with OpenClaw](https://img.shields.io/badge/works%20with-OpenClaw-blue)

把 [OpenClaw](https://github.com/openclaw) 的记忆能力接到 [HCC (Hanyan Cognitive Core)](https://github.com/icemaple77/HCC) 的 REST 网关上：混合检索（BM25 + 向量 + RRF）、`session_start` 自动记忆回顾 + 情绪 warm-start、**每轮 turn-tail 记忆注入**（`appendContext`）、事件持久化、SSE 事件流监听、故障回灌与备份。

本插件与 HCC 主项目相互独立部署——HCC 是跑在任意主机上的 REST 服务，本插件是运行在 **OpenClaw 所在主机**上的客户端集成，通过 HTTP 调用 HCC 的 `/api/v1/*` 端点，两者不需要在同一台机器上。MIT 开源，定位是可以直接进 OpenClaw 插件市场/社区插件列表的独立分发单元——装上即用，不需要改 OpenClaw 本体代码。

## 组成

| 文件 | 类型 | 作用 |
|:-----|:-----|:-----|
| `index.js` | OpenClaw 插件入口 | 注册 `memory_search` / `memory_get` 工具 + `session_start` / `before_prompt_build`（×2 监听器）/ `session_end` / `before_compaction` / `tool_result_persist` 钩子 + `registerMemoryCapability` 记忆后端 |
| `openclaw.plugin.json` | 插件清单 | OpenClaw 加载插件用的元数据 + 配置 schema |
| `package.json` | npm 清单 | 声明 `openclaw.extensions` 入口 |
| `sse_monitor.py` | 常驻脚本 | 订阅 HCC `/api/v1/events/stream`，把 store/update/delete 事件实时归档到本地日志 |
| `hcc_health_probe.py` | 常驻脚本 | 每 30s 探测 HCC `/api/v1/health`，检测挂掉/半死状态，写状态文件供其他脚本判断 |
| `hcc_backup.py` | 定时脚本 | 分页拉取 HCC 全部记忆，存为本地 JSONL 快照，保留最近 14 天 |
| `hcc_backfill.py` | 手动/定时脚本 | HCC 故障期间本地产生的记忆事件，恢复后批量回灌回 HCC |
| `DESIGN.md` | 设计文档 | dreaming 对齐设计 + HCC 不可用时的 fallback 方案（含健康探针/本地暂存/回灌） |

## 功能

- **memory_search / memory_get 工具**：模型可在对话中主动调用，走 HCC 的 `/memory/hybrid-search`（BM25 + pgvector + RRF 融合）
- **`session_start` 自动 recall + 情绪 warm-start**：新会话开始时，插件自动向 HCC 拉取该 user/agent 范围内按 importance 排序的近期记忆（默认 top 5）和当前情绪状态，渲染成一段系统上下文，由第一个 `before_prompt_build` 监听器一次性注入（`prependSystemContext`），不会每轮重复注入；命中的记忆会异步 `touch`（计入 access_count，供遗忘引擎参考）
- **每轮 turn-tail 记忆注入**：第二个 `before_prompt_build` 监听器把 HCC `/context` 检索结果拼进当轮 user 消息尾部（`appendContext`），**不进 system prompt**，因此不破坏 DeepSeek 等提供商的 system-prompt 前缀缓存；3 轮内命中缓存不重复拉取（`APPEND_CONTEXT_THROTTLE_TURNS`），最长 1500 字符（`APPEND_CONTEXT_MAX_CHARS`）
- **自动记忆持久化钩子**：`session_end`（会话结束摘要，并把摘要文本回写 `/emotion/update` 驱动情绪演化）、`before_compaction`（压缩前快照）、`tool_result_persist`（工具结果，低权重存入，供本地降噪模型复核）
- **记忆后端接管**：`registerMemoryCapability` 把 HCC 接入 OpenClaw 的记忆后端接口（search/readFile/status），供内置记忆检索路径调用。生产配置中 `plugins.slots.memory: "hcc-memory"`，即本插件独占 memory 槽位（memory-core 关闭）
- **旁路运维脚本**：SSE 监听、健康探针、每日备份、故障回灌，均为独立于插件本体运行的 Python 脚本（仅标准库），便于用 cron / systemd / launchd 常驻

## 安装

1. 把本目录复制或软链到 OpenClaw 的插件目录（具体路径取决于你的 OpenClaw 版本和配置，本仓库生产部署路径为 `~/hcc-openclaw-plugin`）。
2. 确认 `package.json` 中的 `openclaw.extensions` 指向 `index.js`。
3. 在 OpenClaw 的插件配置里给 `hcc-memory` 传入 `configSchema` 声明的字段（见下）。
4. 启动 OpenClaw，插件会在 `onStartup` 时自动激活。

## 配置

插件配置（`openclaw.plugin.json` 的 `configSchema`）优先级：`pluginConfig` > 环境变量 > 内置默认值。

| 配置项 | 环境变量 | 默认值 | 说明 |
|:-------|:---------|:-------|:-----|
| `baseUrl` | `HCC_BASE_URL` | `http://100.66.103.69:8000` | HCC 网关地址，跨主机部署时必须显式配置为 HCC 实际监听地址 |
| `userId` | `HCC_USER_ID` | `michael` | 记忆归属的 user_id |
| `agentId` | `HCC_AGENT_ID` | `openclaw` | 记忆归属的 agent_id |
| `sessionRecallEnabled` | `HCC_SESSION_RECALL_DISABLED` | `true`（启用） | 关闭则设为 `false` / 环境变量 `1` |
| `sessionRecallLimit` | `HCC_SESSION_RECALL_LIMIT` | `5` | 自动回顾注入的记忆条数上限 |
| `emotionEnabled` | `HCC_EMOTION_DISABLED` | `true`（启用） | 情绪 warm-start / `session_end` 情绪回写开关 |
| `fetchTimeoutMs` | `HCC_FETCH_TIMEOUT_MS` | `8000` | 单次 HCC API 调用的中止超时（毫秒），防止 HCC 半死时拖住 prompt 构建 |

旁路脚本（`sse_monitor.py` / `hcc_health_probe.py` / `hcc_backup.py` / `hcc_backfill.py`）各自读取以下环境变量，均有 `100.66.103.69:8000` 兜底默认值：

| 脚本 | 环境变量 |
|:-----|:---------|
| `sse_monitor.py` | `HCC_STREAM_URL`（完整事件流 URL，非 base URL） |
| `hcc_health_probe.py` | `HCC_HEALTH_URL`（完整健康检查 URL） |
| `hcc_backup.py` | `HCC_BASE_URL` |
| `hcc_backfill.py` | `HCC_BASE_URL`, `HCC_USER_ID`, `HCC_AGENT_ID` |

## 钩子说明

- **`session_start`**：新会话开始时拉取 HCC 记忆回顾 + 情绪状态，暂存待注入；同时把命中的记忆异步 `touch`（access_count+1，供遗忘引擎参考）。注意：OpenClaw 的 session_start payload **没有 `reason` 字段**（只有 sessionId/sessionKey/resumedFrom），且只在 `isNewSession` 时触发——所以旧版 `reason in (new/reset/daily)` 门控是死代码（永不匹配），现已移除，改用 `resumedFrom === sessionId` 防御同会话边界
- **`before_prompt_build`（监听器 1）**：消费 `session_start` 暂存的上下文，通过 `prependSystemContext` 注入一次（每个会话只注入一次，不会每轮重复）
- **`before_prompt_build`（监听器 2）**：每轮用当前用户消息作 query 调 HCC `/context`，结果通过 `appendContext` 拼到当轮 user 消息尾部；3 轮节流 + 1500 字符上限；HCC 请求失败时保留旧块并立即重试，请求成功但无相关记忆时清掉旧块（避免注入与当前话题无关的陈旧记忆）
- **`session_end`**：会话结束时，把 session 摘要（消息数、时长、结束原因）存入 HCC，`type=session`；同一段摘要文本再喂给 `/emotion/update`，驱动情绪状态演化，供下次 `session_start` 读取；同时清理该会话的两个缓存条目
- **`before_compaction`**：OpenClaw 触发上下文压缩前，记录压缩前的消息数/token 数快照
- **`tool_result_persist`**：每条工具调用结果以 `importance=0.3` 低权重存入 HCC，`type=tool_result`——配合 HCC 侧的本地模型降噪（见主仓库 `core/local_filter.py`），噪音结果会被异步复核并标记 `status=discarded`，不进入检索结果

## 错误处理与缓存

- 所有 HCC 调用带 8s 中止超时（`AbortSignal.timeout`），HCC 挂起/半死不会拖住 OpenClaw 的 prompt 构建或会话生命周期
- 两个按会话键控的缓存（`pendingSessionContext`、`turnContextCache`）均设 **100 会话 FIFO 上限**，超出逐出最旧项，防止网关长期运行内存泄漏；`session_end` 时主动清理对应会话条目
- 所有钩子内的 HCC 失败均被 try/catch 兜住并 `log.error`，绝不抛出到 OpenClaw 主流程；工具调用失败返回结构化 `{ error }` 结果而非抛异常
- `memory_get` 按 id 查找时先扫最近 500 条（HCC 无 GET /memory/{id} 端点），未命中再退回内容检索

## 已知限制

- `memory_get` 的 id 精确查找依赖作用域内（user+agent）空查询搜索的分页窗口（每页 100、最多 1000 条）——HCC API 没有按 id 直取的端点，且 `/memory/search` 的 limit 上限为 100（`MemorySearch.le=100`）；超过 1000 条窗口的旧记忆 id 只能靠内容检索兜底（uuid 一般不在 content 里，兜底命中率低）。注意不要改用 `/memory/recent`：它是全局未作用域的最近列表，可能根本不含本 agent 的记忆
- 旁路脚本假设 OpenClaw workspace 位于 `~/.openclaw/workspace`（写日志/状态文件用），如果你的 OpenClaw 安装路径不同，需要相应修改脚本内的 `WORKSPACE` / `LOG_DIR` 常量
- 每轮 turn-tail 注入依赖 OpenClaw 把 `appendContext` 拼进 user 消息尾部（`dist/prepare.runtime-*.js` 的 `preparedPrompt = preparedPrompt + "\n\n" + hookResult.appendContext`）；若未来 OpenClaw 改变此行为，注入位置会随之变化，需回归验证

## 依赖

- Node.js ≥ 18（`AbortSignal.timeout` 需要；OpenClaw 2026.7.1 自带运行时即可）
- Python 3.8+（旁路脚本仅用标准库 `urllib.request`，无第三方依赖）

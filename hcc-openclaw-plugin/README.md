# hcc-memory — HCC × OpenClaw 插件

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](../LICENSE)
![Works with OpenClaw](https://img.shields.io/badge/works%20with-OpenClaw-blue)

把 [OpenClaw](https://github.com/openclaw) 的记忆能力接到 [HCC (Hanyan Cognitive Core)](../README.md) 的 REST 网关上：混合检索（BM25 + 向量 + RRF）、`session_start` 自动记忆回顾 + 情绪 warm-start、事件持久化、SSE 事件流监听、故障回灌与备份。

本目录与仓库根目录的 HCC 主项目相互独立部署——HCC 是一个跑在任意主机上的 REST 服务，本插件是运行在 **OpenClaw 所在主机**上的客户端集成，通过 HTTP 调用 HCC 的 `/api/v1/*` 端点，两者不需要在同一台机器上。MIT 开源，定位是可以直接进 OpenClaw 插件市场/社区插件列表的独立分发单元——装上即用，不需要改 OpenClaw 本体代码。

## 性能与成本

HCC 主仓库跑生产 Agent 的真实 30 天账单：**Prompt 缓存命中率 98%**、总成本 ¥285.67（日均 ~¥9.5）、embedding 走本地 Ollama 零 API 成本。完整数据和结构分析见 [根 README「性能与成本」](../README.md#性能与成本)——省钱的核心逻辑同样适用于接了本插件的 OpenClaw：`session_start` 自动 recall 复用的是结构稳定的上下文，天然吃得到缓存价。

## 组成

| 文件 | 类型 | 作用 |
|:-----|:-----|:-----|
| `index.js` | OpenClaw 插件入口 | 注册 `memory_search` / `memory_get` 工具 + `session_start`（自动 recall + 情绪 warm-start）/ `before_prompt_build`（注入）/ `session_end`（含情绪回写）/ `before_compaction` / `tool_result_persist` 钩子 + `registerMemoryCapability` 记忆后端 |
| `openclaw.plugin.json` | 插件清单 | OpenClaw 加载插件用的元数据 + 配置 schema |
| `package.json` | npm 清单 | 声明 `openclaw.extensions` 入口 |
| `sse_monitor.py` | 常驻脚本 | 订阅 HCC `/api/v1/events/stream`，把 store/update/delete 事件实时归档到本地日志 |
| `hcc_health_probe.py` | 常驻脚本 | 每 30s 探测 HCC `/api/v1/health`，检测挂掉/半死状态，写状态文件供其他脚本判断 |
| `hcc_backup.py` | 定时脚本 | 分页拉取 HCC 全部记忆，存为本地 JSONL 快照，保留最近 14 天 |
| `hcc_backfill.py` | 手动/定时脚本 | HCC 故障期间本地产生的记忆事件，恢复后批量回灌回 HCC |

## 功能

- **memory_search / memory_get 工具**：模型可在对话中主动调用，走 HCC 的 `/memory/hybrid-search`（BM25 + pgvector + RRF 融合）
- **`session_start` 自动 recall + 情绪 warm-start**：新会话（`reason` 为 `new`/`reset`/`daily`）开始时，插件自动向 HCC 拉取该 user/agent 范围内按 importance 排序的近期记忆（默认 top 5）和当前 6 维情绪状态，渲染成一段系统上下文，在下一次 `before_prompt_build` 时一次性注入（`prependSystemContext`），不会每轮重复注入；命中的记忆会异步 `touch`（计入 access_count，供遗忘引擎参考）
- **自动记忆持久化钩子**：`session_end`（会话结束摘要，并把摘要文本回写 `/emotion/update` 驱动情绪演化）、`before_compaction`（压缩前快照）、`tool_result_persist`（工具结果，低权重存入，供本地降噪模型复核）
- **记忆后端接管**：`registerMemoryCapability` 把 HCC 接入 OpenClaw 的记忆后端接口（search/readFile/status），供内置记忆检索路径调用
- **旁路运维脚本**：SSE 监听、健康探针、每日备份、故障回灌，均为独立于插件本体运行的 Python 脚本，便于用 cron / systemd / launchd 常驻

## 安装

1. 把本目录复制或软链到 OpenClaw 的插件目录（具体路径取决于你的 OpenClaw 版本和配置）。
2. 确认 `package.json` 中的 `openclaw.extensions` 指向 `index.js`。
3. 在 OpenClaw 的插件配置里给 `hcc-memory` 传入 `configSchema` 声明的字段（见下）。
4. 启动 OpenClaw，插件会在 `onStartup` 时自动激活。

## 配置

插件配置（`openclaw.plugin.json` 的 `configSchema`）优先级：`pluginConfig` > 环境变量 > 内置默认值。

| 配置项 | 环境变量 | 默认值 | 说明 |
|:-------|:---------|:-------|:-----|
| `baseUrl` | `HCC_BASE_URL` | `http://localhost:8000` | HCC 网关地址，跨主机部署时必须显式配置为 HCC 实际监听地址 |
| `userId` | `HCC_USER_ID` | `default` | 记忆归属的 user_id |
| `agentId` | `HCC_AGENT_ID` | `openclaw` | 记忆归属的 agent_id |

`session_start` 自动 recall / 情绪 warm-start 的开关目前只读环境变量（未声明在 `openclaw.plugin.json` 的 `configSchema` 里，`pluginConfig` 等价字段暂不保证生效）：

| 环境变量 | 默认值 | 说明 |
|:---------|:-------|:-----|
| `HCC_SESSION_RECALL_DISABLED` | 未设置（启用） | 设为 `1`/`true` 关闭 `session_start` 自动记忆回顾 |
| `HCC_SESSION_RECALL_LIMIT` | `5` | 自动回顾注入的记忆条数上限 |
| `HCC_EMOTION_DISABLED` | 未设置（启用） | 设为 `1`/`true` 关闭情绪 warm-start / `session_end` 情绪回写 |

旁路脚本（`sse_monitor.py` / `hcc_health_probe.py` / `hcc_backup.py` / `hcc_backfill.py`）各自读取以下环境变量，均有 `localhost:8000` 兜底默认值：

| 脚本 | 环境变量 |
|:-----|:---------|
| `sse_monitor.py` | `HCC_STREAM_URL`（完整事件流 URL，非 base URL） |
| `hcc_health_probe.py` | `HCC_HEALTH_URL`（完整健康检查 URL） |
| `hcc_backup.py` | `HCC_BASE_URL` |
| `hcc_backfill.py` | `HCC_BASE_URL`, `HCC_USER_ID`, `HCC_AGENT_ID` |

## 钩子说明

- **`session_start`**：新会话开始（`reason` 为 `new`/`reset`/`daily`）时拉取 HCC 记忆回顾 + 情绪状态，暂存待注入；同时把命中的记忆异步 `touch`（access_count+1，供遗忘引擎参考）
- **`before_prompt_build`**：消费 `session_start` 暂存的上下文，通过 `prependSystemContext` 注入一次（每个会话只注入一次，不会每轮重复）
- **`session_end`**：会话结束时，把 session 摘要（消息数、时长、结束原因）存入 HCC，`type=session`；同一段摘要文本再喂给 `/emotion/update`，驱动情绪状态演化，供下次 `session_start` 读取
- **`before_compaction`**：OpenClaw 触发上下文压缩前，记录压缩前的消息数/token 数快照
- **`tool_result_persist`**：每条工具调用结果以 `importance=0.3` 低权重存入 HCC，`type=tool_result`——配合 HCC 侧的本地模型降噪（见主仓库 `core/local_filter.py`），噪音结果会被异步复核并标记 `status=discarded`，不进入检索结果

## 已知限制

- **`kind: "memory"` 与 `registerMemoryCapability` 目前是无条件生效的**：`index.js` 顶部虽然留有一段注释说 `kind: "memory"` "deliberately omitted"（未声明，避免独占 OpenClaw 的 `plugins.slots.memory` 槽位），但紧接着的对象字面量和 `openclaw.plugin.json` 里实际都写了 `kind: "memory"`，`registerMemoryCapability` 也是无条件调用（只用 try/catch 兜底，不判断槽位归属）——即注释描述的"默认不独占"状态和当前代码不一致，实际效果是这个插件会去竞争/占用 OpenClaw 的 memory slot。接入前建议先确认这是否是你想要的行为，需要的话把 `kind: "memory"` 从两个文件里都去掉即可恢复"不独占"状态。
- 旁路脚本假设 OpenClaw workspace 位于 `~/.openclaw/workspace`（写日志/状态文件用），如果你的 OpenClaw 安装路径不同，需要相应修改脚本内的 `WORKSPACE` / `LOG_DIR` 常量。

## 依赖

- Node.js（OpenClaw 插件运行时自带，无需额外安装）
- Python 3.8+（旁路脚本仅用标准库 `urllib.request`，无第三方依赖）

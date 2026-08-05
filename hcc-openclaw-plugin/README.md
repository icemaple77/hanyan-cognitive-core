# hcc-memory — HCC × OpenClaw 插件

把 [OpenClaw](https://github.com/openclaw) 的记忆能力接到 [HCC (Hanyan Cognitive Core)](../README.md) 的 REST 网关上：混合检索（BM25 + 向量 + RRF）、事件持久化、SSE 事件流监听、故障回灌与备份。

本目录与仓库根目录的 HCC 主项目相互独立部署——HCC 是一个跑在任意主机上的 REST 服务，本插件是运行在 **OpenClaw 所在主机**上的客户端集成，通过 HTTP 调用 HCC 的 `/api/v1/*` 端点，两者不需要在同一台机器上。

## 组成

| 文件 | 类型 | 作用 |
|:-----|:-----|:-----|
| `index.js` | OpenClaw 插件入口 | 注册 `memory_search` / `memory_get` 工具 + `session_end` / `before_compaction` / `tool_result_persist` 钩子 + 可选的 `registerMemoryCapability` 记忆后端 |
| `openclaw.plugin.json` | 插件清单 | OpenClaw 加载插件用的元数据 + 配置 schema |
| `package.json` | npm 清单 | 声明 `openclaw.extensions` 入口 |
| `sse_monitor.py` | 常驻脚本 | 订阅 HCC `/api/v1/events/stream`，把 store/update/delete 事件实时归档到本地日志 |
| `hcc_health_probe.py` | 常驻脚本 | 每 30s 探测 HCC `/api/v1/health`，检测挂掉/半死状态，写状态文件供其他脚本判断 |
| `hcc_backup.py` | 定时脚本 | 分页拉取 HCC 全部记忆，存为本地 JSONL 快照，保留最近 14 天 |
| `hcc_backfill.py` | 手动/定时脚本 | HCC 故障期间本地产生的记忆事件，恢复后批量回灌回 HCC |

## 功能

- **memory_search / memory_get 工具**：模型可在对话中主动调用，走 HCC 的 `/memory/hybrid-search`（BM25 + pgvector + RRF 融合）
- **自动记忆持久化钩子**：`session_end`（会话结束摘要）、`before_compaction`（压缩前快照）、`tool_result_persist`（工具结果，低权重存入，供本地降噪模型复核）
- **可选记忆后端接管**：`registerMemoryCapability` 允许 HCC 完全替代 OpenClaw 内置的 memory-core（默认不启用，见 `index.js` 顶部注释）
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

旁路脚本（`sse_monitor.py` / `hcc_health_probe.py` / `hcc_backup.py` / `hcc_backfill.py`）各自读取以下环境变量，均有 `localhost:8000` 兜底默认值：

| 脚本 | 环境变量 |
|:-----|:---------|
| `sse_monitor.py` | `HCC_STREAM_URL`（完整事件流 URL，非 base URL） |
| `hcc_health_probe.py` | `HCC_HEALTH_URL`（完整健康检查 URL） |
| `hcc_backup.py` | `HCC_BASE_URL` |
| `hcc_backfill.py` | `HCC_BASE_URL`, `HCC_USER_ID`, `HCC_AGENT_ID` |

## 钩子说明

- **`session_end`**：会话结束时，把 session 摘要（消息数、时长、结束原因）存入 HCC，`type=session`
- **`before_compaction`**：OpenClaw 触发上下文压缩前，记录压缩前的消息数/token 数快照
- **`tool_result_persist`**：每条工具调用结果以 `importance=0.3` 低权重存入 HCC，`type=tool_result`——配合 HCC 侧的本地模型降噪（见主仓库 `core/local_filter.py`），噪音结果会被异步复核并标记 `status=discarded`，不进入检索结果

## 已知限制

- `registerMemoryCapability`（完全接管 OpenClaw 内置 memory-core）目前**默认关闭**：`index.js` 顶部的 `kind: "memory"` 一旦启用会把整个插件放进 OpenClaw 的 `plugins.slots.memory` 独占槽位（只有槽位持有者会被加载），需要谨慎评估后再启用，详见 `index.js` 内注释。
- 旁路脚本假设 OpenClaw workspace 位于 `~/.openclaw/workspace`（写日志/状态文件用），如果你的 OpenClaw 安装路径不同，需要相应修改脚本内的 `WORKSPACE` / `LOG_DIR` 常量。

## 依赖

- Node.js（OpenClaw 插件运行时自带，无需额外安装）
- Python 3.8+（旁路脚本仅用标准库 `urllib.request`，无第三方依赖）

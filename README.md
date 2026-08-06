# HCC — Hanyan Cognitive Core（含烟认知核心）

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
![Works with OpenClaw](https://img.shields.io/badge/works%20with-OpenClaw-blue)
![Works with Hermes](https://img.shields.io/badge/works%20with-Hermes-blue)
![Works with Claude Code](https://img.shields.io/badge/works%20with-Claude%20Code%20(MCP)-blueviolet)

**跨 Agent 统一记忆层**：一个独立部署的 REST 服务，把记忆存储、混合检索、知识图谱、情绪状态、梦境式记忆巩固、Obsidian 双向同步统一收拢在一个数据库和一套 API 之后，供 Hermes、OpenClaw、Claude Code 等任意 Agent 通过 HTTP 接入。

HCC 不是某个 Agent 框架的内置记忆插件，而是一个**独立于任何具体 Agent 运行时**的认知操作系统：Agent 只需要会发 HTTP 请求，就能获得持久记忆、语义检索、情绪连续性和夜间自动整理。**Works with OpenClaw / Hermes / Claude Code** —— 已提供 OpenClaw 官方插件（`hcc-memory`）与 Claude Code MCP server，其余 Agent 运行时只要能发 HTTP 请求即可接入。

## 关于这个项目

HCC 主体 **MIT 开源**：核心记忆层、混合检索、知识图谱、梦境巩固、情绪引擎、Obsidian 双向同步全部开源可用，可以直接 fork、自部署、二次开发，没有闭源核心。

`hcc-openclaw-plugin/`（`hcc-memory`）是这套记忆层在 OpenClaw 生态里的具体落地：独立插件包、标准 `openclaw.plugin.json` 清单、声明式 `configSchema`，定位是能进入 OpenClaw 插件市场/社区插件列表的独立分发单元，装上即用，不需要改 OpenClaw 本体代码。同样的「REST 网关 + 轻客户端」模式也能给 Hermes、Claude Code（MCP）或任意会发 HTTP 请求的 Agent 提供记忆能力——**开源核心 + 多端插件分发**，是这个项目的长期定位。

---

## 架构

```
                         ┌─────────────────────────────┐
                         │        Agent 层               │
                         │  Hermes / OpenClaw / Claude   │
                         │  Code / 任意会发 HTTP 的 Agent   │
                         └───────────────┬───────────────┘
                                         │ REST / SSE
                         ┌───────────────▼───────────────┐
                         │      Gateway API（FastAPI）     │
                         │   /api/v1/* ，统一入口 + 路由     │
                         └───┬─────┬─────┬─────┬─────┬────┘
                             │     │     │     │     │
              ┌──────────────┘     │     │     │     └──────────────┐
              ▼                    ▼     ▼     ▼                    ▼
        ┌───────────┐   ┌──────────┐ ┌──────┐ ┌────────────┐ ┌───────────┐
        │  Memory   │   │  Dream   │ │Emotion│ │  EventBus  │ │    MCP    │
        │ 混合检索/CRUD │   │三阶段巩固  │ │6维状态机│ │ Redis Pub/Sub│ │ stdio 协议 │
        └─────┬─────┘   └────┬─────┘ └──┬───┘ └──────┬─────┘ └───────────┘
              │              │          │            │
              └──────┬───────┴──────────┴────────────┘
                     ▼
         ┌───────────────────────┐        ┌─────────────────────┐
         │   PostgreSQL 17        │        │   本地降噪（可选）      │
         │   + pgvector           │◄───────┤ Ollama qwen3.5 异步复核│
         └───────────┬───────────┘        └─────────────────────┘
                     │
                     ▼ Sync Engine（双向）
         ┌───────────────────────┐
         │  Obsidian Vault         │
         │  QMD 知识文档 / 梦境日记 /  │
         │  per-agent 导出 / vault API │
         └───────────────────────┘
```

- **Gateway API**：FastAPI 单入口，所有模块通过 `/api/v1/*` 暴露，CORS 开放
- **Memory**：记忆 CRUD + 三种检索模式（关键词 / 向量 / 混合 BM25+向量+RRF）
- **Dream**：夜间三阶段记忆巩固（Light 聚合 → REM 聚类 → Deep 去重生成知识），带 Obsidian 梦境日记
- **Emotion**：6 维度情绪引擎（happiness/curiosity/fatigue/worry/closeness/focus）+ 具名状态机，随对话/记忆/梦境事件演化
- **EventBus**：Redis Pub/Sub（可选，未启用时退化为进程内内存广播），驱动情绪联动、降噪复核、SSE 推送
- **Sync**：PostgreSQL ↔ Markdown 双向同步引擎，定时 + 事件触发
- **Obsidian**：QMD 知识文档生成、per-agent 记忆导出、只读 vault 浏览 API、梦境日记写入
- **本地降噪**：异步订阅记忆写入事件，用本地 Ollama 模型复核低置信度记忆（`tool_result`/插件写入），软删除噪音，不阻塞主写入路径
- **MCP**：stdio 协议 server，把核心记忆工具暴露给支持 MCP 的客户端（Claude Code 等）

---

## 功能特性

- **混合检索**：BM25（PostgreSQL 全文 + jieba 中文分词）+ pgvector 向量检索，RRF（Reciprocal Rank Fusion）融合排序，可选 Qwen3 交叉编码器重排（`HCC_RERANK_ENABLED`）
- **本地模型降噪**：低置信度记忆（工具调用结果、第三方插件写入）异步过 Ollama `qwen3.5:4b` 复核，噪音软删除（`status=discarded`），从不阻塞写入、从不硬删除
- **梦境三阶段**：Light（短周期聚合）→ REM（跨天标签聚类找主题）→ Deep（去重生成知识 + 写入 Memory + 梦境日记），全部幂等（每天每阶段只跑一次，`force` 可强制重跑）
- **情绪 6 维状态机**：happiness / curiosity / fatigue / worry / closeness / focus 连续维度 + 具名复合状态（如"雀跃"/"低落"），检索结果按情绪心境一致性加权，梦境 Deep 阶段可反向调整情绪基线
- **Obsidian 双向导出**：知识文档（QMD）自动生成、per-agent 人类可读记忆导出、梦境日记（叙事 + 审计报告双文件）、孤儿文档自动归档
- **只读 vault API**：`/vault/list` / `/vault/read`，路径严格限制在 `HCC_VAULT_ROOT` 内（拒绝 `..`、拒绝逃逸的软链、拒绝绝对路径）
- **SSE 多端感知**：`/events/stream` 推送记忆变更事件（store/update/delete），供旁路监听器、多客户端保持状态同步
- **Redis EventBus**：可选，未配置时优雅降级为进程内内存事件总线，模块间零耦合

---

## 性能与成本

以下是作者本人生产环境跑 HCC 的真实 30 天用量数据（DeepSeek API 账单，不是营销基准测试）：

| 指标 | 数值 |
|:-----|:-----|
| 30 天总成本 | ¥285.67 |
| 日均成本 | ~¥9.5 |
| **Prompt 缓存命中率** | **98%**（6313.6M tokens 命中 / 126.8M 未命中） |
| 输出 tokens | 13.6M |
| 模型成本占比 | flash ¥277.43（97%）+ pro ¥8.24（3%） |

**为什么这么便宜：**

- **98% 缓存命中率是结构性的，不是偶然**——HCC 把记忆检索结果、系统上下文按稳定结构拼装复用，重复上下文命中缓存价，而不是每轮都按原价 token 重新计费
- **本地 embedding，0 API 成本**——向量化默认走本地 Ollama（`qwen3-embedding:0.6b`，1024 维，中文语义强于 `nomic-embed-text`），混合检索/语义检索不产生任何 embedding API 调用费用
- **无 OCR、无强制云端依赖**——PDF/文档索引走本地 `pdf-inspector` 解析，本地降噪走本地 Ollama 模型复核低置信度记忆；除非你自己接了云端 LLM API，整条记忆链路可以完全离线运行，跑不出账单
- **BM25 + 向量混合检索**（RRF 融合，可选 Qwen3 交叉编码器重排）——大部分召回不需要触发昂贵的语义精排，进一步压低单次调用成本

> 数据来自 [icemaple77](https://github.com/icemaple77) 本人真实生产 Agent 的账单快照（2026-08），具体成本因你接入的模型、调用量、上下文结构而异，仅供参考。

---

## 快速开始

### 依赖

- Python 3.11+
- PostgreSQL 17 + [pgvector](https://github.com/pgvector/pgvector) 扩展
- Redis（可选，未启用时事件总线退化为进程内内存广播）
- Ollama（可选，本地降噪 / 本地 embedding 需要；不装也能跑，`HCC_EMBEDDING_PROVIDER=hash` 走零依赖哈希 embedding）

### 安装

**方式一：一键安装脚本（推荐）**

```bash
git clone https://github.com/icemaple77/hanyan-cognitive-core.git
cd hanyan-cognitive-core
./install.sh
```

`install.sh` 会依次检测/安装 Python 3.11+、PostgreSQL 17 + pgvector（有 Docker 优先用 `docker compose` 一键起 pgvector 容器，没有则走 Homebrew/apt 原生安装）、可选 Redis/Ollama，创建 venv 装依赖，生成 `.env`，最后建表——幂等，重复运行安全。可选参数：`--full`（含 rerank + PDF extras）、`--skip-db` / `--skip-redis` / `--skip-ollama`，完整说明见 `./install.sh --help`。

**方式二：手动安装**

```bash
git clone https://github.com/icemaple77/hanyan-cognitive-core.git
cd hanyan-cognitive-core

# 用 uv（推荐，仓库自带 uv.lock）
uv sync

# 或用 pip
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

### 配置

```bash
cp .env.example .env
# 至少确认 HCC_DATABASE_URL 指向可用的 PostgreSQL 实例
```

关键环境变量（完整列表见 `.env.example`，以及 `gateway/core/config.py` / `core/config.py` 里每项的详细说明）：

| 变量 | 说明 |
|:-----|:-----|
| `HCC_DATABASE_URL` | PostgreSQL 连接串（必需） |
| `HCC_QMD_DIR` | Obsidian 知识文档输出目录 |
| `HCC_REDIS_ENABLED` | Redis EventBus 开关，默认 `false`（进程内内存广播） |
| `HCC_NOISE_FILTER_ENABLED` | 本地降噪开关，默认 `true`（需要本地 Ollama） |
| `HCC_RERANK_ENABLED` | 混合检索交叉编码器重排开关，默认 `false` |
| `HCC_VAULT_ROOT` | Obsidian vault 根目录，供 `/vault/*` API 和梦境日记使用 |

### 启动数据库（Docker，可选）

```bash
docker compose up -d db redis   # 仅启动依赖，不用容器跑 HCC 本体
```

也可以直接用本机已有的 PostgreSQL/Redis，只要在 `.env` 里指对连接串即可——本地开发无需 Docker。

### 启动 HCC

```bash
uv run uvicorn gateway.main:app --reload --host 0.0.0.0 --port 8000
# 或用 Makefile
make dev
```

首次启动会自动建表（SQLAlchemy metadata create_all）并起 3 个梦境后台循环 + 同步循环（受 `HCC_DREAM_AUTO_ENABLED` / `HCC_SYNC_AUTO_ENABLED` 控制）。

### 健康检查

```bash
curl http://localhost:8000/api/v1/health
# → {"status":"ok","version":"0.1.0","service":"hanyan-cognitive-core"}
```

### 容器化部署（可选）

仓库提供 `Dockerfile` + `docker-compose.yml`（api + mcp + db + redis 四个服务），适合部署到常驻服务器：

```bash
docker compose up -d
```

---

## API 端点

统一前缀 `/api/v1`。完整 OpenAPI 交互文档见运行中的 `http://localhost:8000/docs`。

### Memory

```
POST /memory/store          — 存入记忆
POST /memory/search         — 关键词搜索（ILIKE，支持 user_id/agent_id/shared 过滤）
POST /memory/update         — 更新
POST /memory/delete         — 删除
POST /memory/touch          — 命中强化（access_count+1, last_access=now）
GET  /memory/recent         — 最近记忆
POST /memory/semantic-search — 纯向量语义搜索
POST /memory/hybrid-search   — BM25 + 向量混合检索，RRF 融合（推荐入口）
```

示例：

```bash
curl -X POST http://localhost:8000/api/v1/memory/hybrid-search \
  -H "Content-Type: application/json" \
  -d '{"query":"上次部署踩的坑","limit":10,"user_id":"me","agent_id":"main"}'
```

### Document（独立知识库检索，非 Memory 表）

```
POST /document/search
POST /document/hybrid-search
GET  /document/recent
```

### Context（单入口自动编排）

```
POST /context   — 自动编排 Memory + Knowledge + Emotion，一次调用拿到组装好的上下文
```

### Graph

```
POST /graph/entity              — 添加实体
POST /graph/relation            — 添加关系
POST /graph/query               — 查询图谱
GET  /graph/entity/{entity_id}  — 实体详情
```

### Emotion

```
GET  /emotion/state       — 当前情绪（完整维度，供 Agent 内部使用）
GET  /emotion/display     — 当前情绪（展示模式，供小屏/UI 使用）
POST /emotion/update      — 从文本触发情绪更新
GET  /emotion/history     — 近期触发日志
GET  /emotion/snapshots   — 每日冷快照（Deep 阶段生成，供心情趋势回顾）
```

### Dream（三阶段记忆巩固）

```
POST /dream/light     — 触发 Light 阶段（幂等：每天每条记忆一次）
POST /dream/rem       — 触发 REM 阶段（幂等：每天一次）
POST /dream/deep      — 触发 Deep 阶段，写入 Memory + 日记（幂等：每天一次）
GET  /dream/status    — 各阶段最近一次运行时间 + 当前阈值配置
```

### Vault（只读 Obsidian 浏览）

```
GET /vault/list?path=   — 列出目录（默认 vault 根目录）
GET /vault/read?path=   — 读取文件内容（路径越权一律拒绝）
```

### Sync / Export

```
POST /sync/qmd            — 手动触发 PostgreSQL → Markdown 同步
GET  /sync/status         — 同步状态
POST /export/agents       — 重新生成 per-agent_id 人类可读导出
```

### Events（SSE）

```
GET /events/stream   — 订阅记忆变更事件流（store/update/delete）
```

### Cognitive（认知子系统）

```
POST /orchestrator/evaluate   — 判断一段内容是否值得存储
GET  /forget/scan             — 遗忘扫描（只读，不写库）
POST /forget/apply            — 执行遗忘（归档，永不物理删除）
GET  /personality/summary     — 人格画像
POST /personality/process     — 处理文本更新人格画像
POST /subconscious/retrieve   — 三层检索（意识/前意识/潜意识）
GET  /router/summary          — 模型调度配置
POST /router/profile          — 切换硬件档位
POST /optimizer/scan          — 扫描可吸收文件
POST /optimizer/run           — 执行完整工作区优化
POST /optimizer/bootstrap     — 生成引导文件
POST /indexer/scan            — 扫描工作区知识文件
POST /indexer/run             — 索引工作区知识
POST /dream/consolidate       — 旧版一次性记忆巩固（向后兼容，新代码用上面的三阶段接口）
```

### MCP 工具（stdio 协议，`mcp/server.py`）

```
store_memory / search_memories / recall / semantic_search /
hybrid_search / get_recent_memories / delete_memory / evaluate
```

在支持 MCP 的客户端（如 Claude Code）里配置 `mcp/server.py` 为 stdio server 即可直接使用。

---

## OpenClaw 接入

`hcc-openclaw-plugin/` 目录是一个独立的 OpenClaw 插件（`hcc-memory`），通过 HTTP 调用 HCC 的 REST API，无需和 HCC 部署在同一台机器，定位是可以直接进 OpenClaw 插件市场/社区插件列表的独立分发单元。详见 [`hcc-openclaw-plugin/README.md`](hcc-openclaw-plugin/README.md)，要点：

- **工具**：`memory_search`（走 `/memory/hybrid-search`，BM25+向量+RRF 融合）、`memory_get`（按 id 或内容匹配）
- **`session_start` 自动 recall + 情绪 warm-start**：新会话开始时自动从 HCC 拉取相关历史记忆（按 importance 排序取 top-N）和当前情绪状态，在下一次 `before_prompt_build` 时一次性注入系统上下文——Agent 不用等用户先问才想起上次聊过什么
- **`session_end` 情绪回写**：会话结束时把摘要文本喂给 `/emotion/update`，情绪状态跨会话连续演化，下次 `session_start` 能读到最新情绪
- **其余钩子**：`before_compaction`（压缩前快照）、`tool_result_persist`（工具结果低权重存入，配合 HCC 侧本地降噪异步复核）
- **配置**：插件 `configSchema` 的 `baseUrl` / `userId` / `agentId`，或对应的 `HCC_BASE_URL` / `HCC_USER_ID` / `HCC_AGENT_ID` 环境变量；`session_start`/情绪相关开关见插件 README

---

## 常见问题 / 部署说明

- **本地开发不需要 Docker**：只要有一个可访问的 PostgreSQL（装了 pgvector 扩展）实例，改好 `HCC_DATABASE_URL` 直接 `uvicorn` 启动即可；Redis、Ollama 均为可选依赖，未配置时对应功能优雅降级或关闭。
- **embedding 零依赖跑通**：`HCC_EMBEDDING_PROVIDER=hash` 用确定性哈希代替真实向量模型，适合先把链路跑通再接真实 embedding（`ollama` 或 `sentence-transformers`）。
- **重排模型是可选加分项**：`HCC_RERANK_ENABLED=false`（默认）时，混合检索仍然是完整的 BM25+向量+RRF 融合，只是不做二次交叉编码器重排。
- **数据库表自动创建**：首次启动 `uvicorn` 会通过 SQLAlchemy `create_all` 自动建表，无需手动跑 migration 脚本即可开发（生产环境建议接入 Alembic，仓库已列出该依赖）。
- **容器化部署**：`docker-compose.yml` 提供 api + mcp + db + redis 四服务栈，适合部署到常驻服务器；`Dockerfile` 仅打包 gateway/core/scanner/mcp，不含开发依赖。

---

## License

[MIT](LICENSE)

# Hanyan Cognitive Core (HCC)

Memory Operating System for AI Agents
认知操作系统 — 记忆/知识/情绪/梦境/人格 统一管理中心

---

## 项目定位

HCC 不是一个 Memory Provider，而是一个 **Workspace Governor（工作区管理器）**。

它接管 AI Agent（OpenClaw、Hermes、Claude Code、Cursor 等）的 Workspace 生命周期：
Agent 写 Markdown → Scanner 吸收 → PostgreSQL 存储 → Optimizer 清场 → 重写引导文件

## 环境

| 项目 | 值 |
|:-----|:----|
| 源码 | `~/workspace/projects/HCC/` |
| 运行位置 | **aicore N100**（Docker，局域网 LAN_HOST，Tailscale TAILSCALE_PEER_HOST） |
| 开发位置 | **Mac Mini M4**（源码编辑 + git） |
| PostgreSQL 数据 | Docker volume `hcc_pgdata`（~47MB） |
| Python | 3.11（项目内 uv 管理） |

## 启动/停止

```bash
# 部署：本地提交 → scp → aicore Docker
cd ~/workspace/projects/HCC
tar czf /tmp/hcc.tar.gz --exclude=.git --exclude=__pycache__ --exclude=.venv .
scp /tmp/hcc.tar.gz aicore:/tmp/
ssh aicore 'rm -rf /home/michael/projects/HCC && mkdir -p /home/michael/projects/HCC && cd /home/michael/projects/HCC && tar xzf /tmp/hcc.tar.gz && docker compose up -d'

# 启动（aicore）
docker compose up -d

# 停止（aicore）
docker compose down

# 重启单服务
docker compose restart api
```

## 验证

```bash
# 健康检查
curl http://aicore:8000/api/v1/health
# → {"status":"ok","version":"0.1.0","service":"hanyan-cognitive-core"}

# Context API（单入口）
curl -X POST http://aicore:8000/api/v1/context \
  -H "Content-Type: application/json" \
  -d '{"query":"BEES部署","user_id":"michael","agent_id":"main","include_emotion":true}'
```

---

## 模块清单（76/76 完成）

### Stage 1-10（核心功能）

| Stage | 模块 | 文件 | 说明 |
|:-----:|:-----|:-----|:------|
| 1 | **Gateway** | `gateway/` | FastAPI，16 个端点 |
| 2 | **Memory** | `gateway/services/` | CRUD + 语义搜索 |
| 3 | **Scanner** | `scanner/` | 文件系统扫描吸收 |
| 4 | **Markdown Sync** | `core/sync_engine.py` | PostgreSQL ↔ Markdown 双向同步 |
| 5 | **Embedding** | `gateway/core/embeddings.py` | hash/ollama/ST 三后端 |
| 6 | **Knowledge Graph** | `core/graph.py` | Entity/Relation 模型 |
| 7 | **Emotion** | `core/emotion.py` | 6 维度情绪引擎 |
| 8 | **Dream** | `core/dream.py` | 聚类/去重/知识生成 |
| 9 | **Planner** | `core/query_planner.py` | 查询分类 + 自动编排 |
| 10 | **Plugin SDK** | `core/providers/` | Provider 接口 + Memory/Knowledge Provider |

### v2 架构增强

| 模块 | 文件 | 说明 |
|:-----|:-----|:------|
| Redis Working Memory | `core/redis_manager.py` | TTL 自动过期 |
| EventBus | `core/event_bus.py` | Redis Pub/Sub |
| QMDGenerator | `core/qmd_generator.py` | PostgreSQL → Markdown |
| SyncEngine | `core/sync_engine.py` | 双向同步 |

### v2.1 架构收敛

| 模块 | 文件 | 说明 |
|:-----|:-----|:------|
| Context API | `gateway/api/context_routes.py` | 单入口 POST /api/v1/context |
| Prompt Builder | `core/prompt_builder.py` | 统一 Prompt 组装 |
| Model Router | `core/model_router.py` | 模块级模型调度 |
| Provider SDK | `core/providers/base.py` | 数据类 + Provider ABC |

### 认知系统

| 模块 | 文件 | 说明 |
|:-----|:-----|:------|
| Forget Engine | `core/forget.py` | importance 衰减 → 归档 → 删除 |
| Memory Orchestrator | `core/orchestrator.py` | 判断什么该记 |
| Personality Engine | `core/personality.py` | 偏好逐步学习（0.3→0.99） |
| Subconscious | `core/subconscious.py` | 意识→前意识→潜意识三层检索 |
| Workspace Optimizer | `core/optimizer.py` | 工作区生命周期管理 |
| Knowledge Indexer | `core/knowledge_indexer.py` | projects/tasks/rules/soul 索引 |

### MCP 协议

| 模块 | 文件 | 说明 |
|:-----|:-----|:------|
| MCP Server | `mcp/server.py` | stdio 协议，5 个工具 |

---

## API 端点（16 个）

### Memory
```
POST /api/v1/memory/store     — 存入记忆
POST /api/v1/memory/search    — 搜索记忆（支持 agent_id/shared 过滤）
POST /api/v1/memory/update    — 更新
POST /api/v1/memory/delete    — 删除
GET  /api/v1/memory/recent    — 最近记忆
POST /api/v1/memory/semantic-search — 向量语义搜索
```

### Context（单入口）
```
POST /api/v1/context          — 自动编排 Memory+Knowledge+Emotion
```

### Graph
```
POST /api/v1/graph/entity     — 添加实体
POST /api/v1/graph/relation   — 添加关系
POST /api/v1/graph/query      — 查询图谱
```

### Emotion
```
GET  /api/v1/emotion/state    — 当前情绪
POST /api/v1/emotion/update   — 更新情绪
```

### Cognitive
```
POST /api/v1/orchestrator/evaluate   — 判断是否存储
GET  /api/v1/forget/scan             — 遗忘扫描
GET  /api/v1/personality/summary     — 人格画像
POST /api/v1/subconscious/retrieve   — 三层检索
GET  /api/v1/router/summary          — 模型调度配置
POST /api/v1/optimizer/run           — 工作区优化
POST /api/v1/indexer/run             — 知识索引
```

### MCP Tools
```
store_memory      — 存入
search_memories   — 搜索
semantic_search   — 语义搜索
get_recent_memories — 最近记忆
delete_memory     — 删除
```

---

## 数据流

```
你说的话
  ↓
Hermes / Agent → POST /api/v1/context
  ↓
Query Planner → Memory + Knowledge + Emotion + Personality
  ↓
返回组装好的 Context
  ↓
Agent 回复后 → POST /api/v1/memory/store（存本次对话）
  ↓
Forget Engine → 跟踪衰减
  ↓
Dream Engine（凌晨）→ 聚类 → 去重 → 生成 Knowledge
  ↓
QMD Generator → 写入 Obsidian（仅 shared=true）
  ↓
Workspace Optimizer → 清场 + 重写引导文件
```

---

## 已知问题

### OpenClaw 兼容性（未解决）

**问题：** OpenClaw 源码写死了读 MEMORY.md / memory/*.md 做事实来源。
替换为 HCC 引导文件后，Agent 初始化报错 `reply session initialization conflicted for agent:main:main`。

**原因：**
- OpenClaw 启动时解析 AGENTS.md，引用到大量不存在的子目录（self-improving/domains/、proactivity/memory/working-buffer.md 等）
- OpenClaw 的 SQLite 数据库（200MB WAL）偶发写入冲突

**当前状态：** workspace 已还原为原始 507 个文件，OpenClaw 可正常运行。
HCC + OpenClaw 集成需等待新版本 OpenClaw 测试。

### 其他

| 问题 | 状态 |
|:-----|:-----|
| `Path.walk()` Python 3.12+ 才支持 | ✅ 已改用 `os.walk()` |
| PostgreSQL 时区 aware/naive 冲突 | ✅ 已修复 |
| pgvector 扩展需手动 enable | ✅ 已加到启动脚本 |
| 大文件内容导致 HTTP 500 | ✅ 已加 `sanitize()` |
| shutil.ignored() Python 3.12+ | ✅ 已改用 `ignore_patterns()` |

---

## git 历史（17 次 commit）

```
58d3423 最终补齐 - Subconscious + ModelRouter + 设计图
0679a2c 多Agent支持 + QMD仅共享知识
1363228 Workspace Optimizer
11a5f05 Forget / Orchestrator / Personality
7f39824 Stage 6+7+8 - Graph / Emotion / Dream
390c12f Phase 3 - Scanner
55b340f Phase 2 - MCP Memory Server
dc54378 Phase 1 - Gateway + pgvector
e3acf8b Knowledge Indexer
...
```

---

## 部署架构

```
Mac Mini M4（开发/OpenClaw）
  └── MLX Qwen3.5-4B（可选，localhost:11435）
  └── OpenClaw Gateway（:18789）

aicore N100（Docker）
  └── HCC API（:8000）
  └── PostgreSQL + pgvector（:5433）
  └── Redis（:6381，可选）
```

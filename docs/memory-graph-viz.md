# 记忆星空图（Memory Graph Visualization）

> HanyanOS docs/14 三「记忆图可视化」落地——借鉴 Ackem 的 d3 force-graph *设计思路*
> （节点=记忆/边=关联/大小=重要性/颜色=情绪），未借用其 AGPL 代码。
> 2026-08-09 实施，生产服务（100.66.103.69:8000，手动 uvicorn，无 launchd）全程未中断。

## 1. 数据源

- 节点数据来自 `memories` 表（`gateway/models/__init__.py::Memory`）：
  `id / type / content / summary / importance / tags / created_at / access_count / embedding(1024维, pgvector)`。
  本地库 2696 条 `status=active` 记忆，2685 条带 embedding。
- **无独立的按记忆情绪字段** —— 情绪是 `core/emotion.py::EmotionEngine` 的**全局状态**（"含烟此刻的心情"），不是每条记忆的标签。
  因此颜色维度改为对每条记忆的 `content+summary` 做**一次性关键词扫描**（复用 `EMOTION_TRIGGERS`/`NEW_DIM_TRIGGERS`/`_is_negated`，纯函数、不碰 EmotionEngine 状态），取主导维度 + valence 作为近似情绪信号。这是已知的简化——真正的按记忆情绪需要在写入路径持久化一份快照，属于后续工作。
- 边数据全部现算，不新增表：语义相似用已有 pgvector 列直接算 cosine 相似度；时间关联用 `created_at` 相邻窗口。

## 2. API

### `GET /api/v1/memory/graph`

| 参数 | 默认 | 说明 |
|---|---|---|
| `user_id` / `agent_id` / `type` | None | 按原有字段过滤 |
| `limit` | 200（上限 200） | 取最近 N 条 active 记忆作为节点集，之后所有边计算都限定在这 N 个 id 内，成本与总表大小无关 |
| `knn` | 5（上限 20） | 每个节点最多保留 k 个语义近邻边（top-K，而非全局阈值——避免高密度区域变成毛球、稀疏区域变孤岛） |
| `min_similarity` | 0.5 | 语义边的相似度下限（本地样本 200 条中位数相似度 ≈0.38，p90≈0.55，p99≈0.74——0.5 大致对应中上游） |
| `temporal_window_hours` | 6.0 | 同 `type` 且时间相邻 ≤ 此窗口的记忆连时间边 |

返回：
```json
{
  "nodes": [{
    "id": "...", "label": "前60字摘要…", "preview": "前200字…",
    "type": "fact", "importance": 0.9, "tags": [...],
    "access_count": 3, "created_at": "2026-08-08T21:57:17",
    "has_embedding": true,
    "emotion": {"dominant": "happiness", "valence": 0.35, "color": "#F5C542"}
  }],
  "edges": [
    {"source": "id1", "target": "id2", "type": "semantic", "weight": 0.68},
    {"source": "id3", "target": "id4", "type": "temporal", "weight": 1.0}
  ],
  "meta": {
    "node_count": 200, "edge_count": 736,
    "semantic_edge_count": 625, "temporal_edge_count": 111,
    "params": {...}
  }
}
```

`emotion.color` 是服务端直接给出的 hex 颜色（17 维情绪主导维度 → 色板，`core/memory_graph.py::DIMENSION_COLOR`），任何消费方（HUD 小组件、下面的 d3 页面、未来的 Obsidian 插件）都不需要重新实现情绪配色表，直接用即可。无关键词命中的记忆 `dominant=null`，颜色为中性灰 `#7C7C88`。

### `GET /api/v1/memory/graph/view`

自包含 HTML 页面（CDN 引入 d3 v7，无需构建步骤），复用 `/graph/export?format=html`（Mermaid 知识图谱查看器）已有的"直接返回 HTML Response"模式。浏览器直接打开：

```
http://100.66.103.69:8000/api/v1/memory/graph/view?limit=200&knn=5
```

页面 JS 会把 querystring 原样转发给 `/api/v1/memory/graph`，所以过滤参数（`type=fact`、`user_id=...`）同样适用于 view。力导向图支持拖拽、缩放、悬停查看摘要/类型/重要性/情绪。

### 接入 HUD

任何前端只需一次 `fetch('/api/v1/memory/graph?limit=...')`，拿到的 `nodes[].emotion.color` / `importance` / `edges[].type` 可直接喂给任意力导向图库（不限 d3——Ackem 用的也是同一套 `radius=weight, color=valence, distance∝1-similarity` 映射）。不需要额外的 WebSocket/HUD 专属协议。

## 3. 实现文件

- `core/memory_graph.py`（新增）—— `build_memory_graph()` 查询 + kNN 语义边 + 时间边 + `_estimate_affect()` 情绪估计。knn 边用 SQLAlchemy `aliased(Memory)` 自连接 + `row_number() OVER (PARTITION BY a.id ORDER BY similarity DESC)` 一次查询搞定，不是 N² 的 Python 循环。
- `gateway/api/memory_routes.py`（改动，已备份为 `memory_routes.py.bak-20260809`）—— 追加 `/memory/graph`、`/memory/graph/view` 两个路由，路由已挂在既有 `/api/v1` + `memory` tag 下，`gateway/main.py` 无需改动。

## 4. 验证结果（本地库真实数据，2026-08-09）

通过 `fastapi.testclient.TestClient`（独立进程，未触碰生产 uvicorn 8000 进程）跑通：

```
GET /api/v1/memory/graph?limit=200&knn=5        → 200 OK
  node_count=200  edge_count=736  semantic=625  temporal=111
GET /api/v1/memory/graph?limit=30&type=fact      → 200 OK
  node_count=24  edge_count=42  semantic=34  temporal=8
GET /api/v1/memory/graph/view                    → 200 OK, text/html, 3840 bytes
```

主导情绪分布抽样（200 节点）：`None(无关键词命中)=162, happiness=12, ecstasy=6, jealousy=6, sadness=4, closeness=3, fatigue=3, shyness=2, anger=2` —— 符合预期：大部分记忆是陈述性内容（工具结果/事实/配置），只有少数带情绪色彩的对话片段会命中关键词。

`app` 对象导入（`from gateway.main import app`）确认两条新路由注册无异常，其余 15 条既有路由不受影响。

## 5. 已知简化 / 后续可做

- 情绪是关键词扫描近似值，不是真情绪快照——要更准可以在写入路径顺手存一份 `EmotionEngine.update()` 产生的 shift 快照到 `Memory` 新列（需要迁移）。
- 没做因果边（causal）——现有数据没有因果标注，强行推断容易是噪声，先留白。
- `limit` 硬上限 200（对齐任务要求"不需要太重"），2696 条记忆里能看到最近的一个切片；如果要做全量鸟瞰图需要分页/聚类降采样，不在本次范围内。

## 6. 上线

代码已加但**生产 uvicorn（PID 见 `ps aux | grep uvicorn`）未重启**，新路由要等下次重启才会在 100.66.103.69:8000 生效——按指示重启时机单独定。

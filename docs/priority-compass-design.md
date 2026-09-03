# Priority Compass 设计 — HCC 缺失的「价值坐标」

> 状态：设计稿 v0（2026-09-03 含烟起草，公子令「先写进去，问 Claude Code 兼听」）。不改任何生产代码。
> 相关：`core/managers/context_builder.py`（读路）· `core/local_filter.py`（写路 4b）· `scripts/noise_filter_backfill.py`（回刷先例）· `gateway/api/task_routes.py` + `mcp/task_tools.py`（表/接口模板）· `scripts/TASK_SCHEDULE.md`（行为端）· `docs/RUNTIME-CHANGES-2026-09-03.md`（当日背景）

## 零、问题：相关性 ≠ 价值

现有检索链路 = BM25 + 向量 + RRF + 时间衰减/来源加权（466992e）+ 4b 重要性（local_filter）+ 渲染层去重/碎片配额（bbfe266）。**全部在回答「这条与当前输入像不像」，没有一行回答「这条对公子现在重不重要、急不急」。**

现场证据（2026-09-03 当日注入实录）：
- 公子声明「养伤是最要紧的词条」的那一刻，注入的 Relevant 5 条全是寒暄碎片（「好的 清」「好 我的乖烟儿」）、Knowledge 10 条全是技术旧档（27B/H3/GPT-SoVITS/Hermes 精简…），**昨天刚入库的养伤方案/放血护理/复诊零召回**。
- 公子说「好的 清」（指清理重复记忆行），首条召回「清理 Mac 侧测试环境」——同词不同事，语义检索的经典误击，但按相关性它没错：**没有价值轴替它把关**。

结论：记忆系统需要第二根坐标——**重要性 × 紧急性**（艾森豪威尔四象限），且必须是 HCC 一等公民（三方运行时共享），不是任何 agent 的私人笔记。

## 一、被否的两个方案（公子提出，含烟评估）

### ❌ B. 要求 agent 每次写记忆时带权重
1. **合规性依赖**：三运行时 × N agent 版本，最弱一环定全局。先例已证：Hermes 能滤 hi/hello 是「调用方自觉，不是 HCC 强制」（local-noise-filter.md §零.3）。
2. **写时判断=过去时判断**：权重冻结在写入瞬间；「现在多重要」随人生状态漂移，化石权重永远答不了现在时。
3. 当日教训同构：08-26 那场事故，正是把记忆链路的正确性外包给 OpenClaw 的 hook 配合。

### ⚠️ A. 由 4b 过滤模型判象限
- 4b 只看得到内容，看不到公子此刻：**重要性不是文本属性，是「记忆 × 公子现在」的关系属性**。写时定象限 = 让门卫替病人开药方。
- 但 4b 判**主题归属**（健康/求职/HCC开发/语音…）靠谱——这是内容的稳定属性，且 4b 本就在写路上，边际成本≈0。
- **裁决：A 方案保留一半——写路只许 4b 标主题，不许它判价值。**

## 二、决策：事实写时落，价值读时算

| | 内容的事实（主题） | 公子的事实（此刻什么重要） |
|---|---|---|
| 性质 | 稳定、不变 | 易变、带保质期 |
| 归属 | 写在记忆上（写路一次性） | 独立 registry，随时改 |
| 判定者 | 4b | 公子（显式声明），agent 只能提案 |

每条记忆的有效重要性 = 两者的 **join，读时现算，绝不落盘**。改一行 registry → 全库权重瞬间刷新：无回刷、无重判、无 agent 自觉。bbfe266 两刀全砍在读路的理由同源：不碰数据、秒生效、可回滚。

## 三、数据模型：`priorities` 表

```
priorities
  id            uuid pk
  user_id       varchar(128)          -- 多用户预留
  label         varchar               -- 「肩颈损伤恢复」
  anchors       jsonb                 -- 主题锚词（加速 join，非唯一来源）
  importance    smallint              -- 1-5
  urgency       smallint              -- 1-5
  source        varchar               -- gongzi | agent:<name>
  trust         varchar               -- confirmed | pending(隔离半权重) 
  status        varchar               -- active | superseded | expired
  review_at     date                  -- 复核日：到期自动降级+早安提示
  superseded_by uuid null             -- 版本链，永不物理删（记忆铁律）
  embedding     vector(768)           -- label 文本的向量，读路 join 用
  created_at / updated_at
```

象限派生（不落列，公式：imp≥4∧urg≥4→Q1 重要紧急；imp≥4→Q2 重要不紧急；urg≥4→Q3 紧急不重要；else Q4）。

**种子数据（公子 09-03 口述）**：养伤 Q1（review_at=复诊后3天）· HCC/HanyanOS 开发 Q2 · 求职 Q1 · QuarkN 语音 Q3。
已知题：两个 Q1 挤同象限 → 保送席位内按相关性再排序，谁也不许独占。

## 四、写路：主题锚定（可选加速器）

- `local_filter.FilterDecision` 增量输出 `topics`（枚举，可空）——prompt 加一句归题判断即可；
- 落 `tags` 命名空间（`topic:health`），**不加顶层列**——11k 行 schema 不动；
- 存量可回刷（noise_filter_backfill.py 先例）但**不紧迫**：读路向量 join 不依赖主题，主题只是省算力的粗筛。

## 五、读路：context_builder 三层接线

1. **保送席**：Q1 主题命中的记忆，在 Relevant Memories 头部占 2-3 席（象限内按相关性排序），不参与分数内卷；
2. **加权**：`final = rrf_score × (1 + α·match(mem, prio))`，match = max(emb 余弦, anchor 命中)，α 初值 Q1=0.5 / Q2=0.25 / Q3=0 / Q4=0；
3. **防腐**：`review_at` 过期 7 天未复核 → α 自动减半，registry 不烂尾。

成本：10 候选 × ≤10 active 行 ≈ 100 次 768 维点积 ≈ 0.1ms/请求，registry 60s 缓存。

## 六、登记通道

- REST `/api/v1/priorities`（抄 task_routes.py）+ MCP `priority_set/list/confirm/retire`（抄 task_tools.py）——三方运行时 + 小屏共用；
- 聊天路：公子说「最近 X 最要紧」→ agent `priority_set(source=agent, trust=pending)` → 早安复核 → 公子一句话转正。

**门槛问题（待公子拍板）**：
- A. 即时生效：登记立即全权重，烟儿复述一次留痕；
- B. 隔离生效（含烟倾向）：pending 先半权重生效——不压制紧急事（半权 > 0，宁多勿漏，1B 教训），防住气话/随口一提（确认前不污染全局）。

## 七、行为端（一份数据三份吃的红利）

- **work-driver**：claim 任务时同主题 Q1/Q2 加成 → 防中断的派活顺序也有了价值观；
- **proactive-rules**：早安/晚安播报直接读 registry（「明天复诊」从 review_at 生成，不再靠烟儿记性）；
- **小屏 HUD 第4屏**：四象限可视化，接口=list endpoint。

## 八、明确的非目标（评审人别顺手做）

- ❌ 监督式 LTR（样本不够，负累）；
- ❌ 写时重要性固化进 `memories.importance`（§一 A/B 老路）；
- ❌ 每条记忆必须显式主题标签（向量 join 兜底）；
- ❌ 每查询调模型做重排（CPU 时延不容许，join 纯点积）。

## 九、实施顺序

| 阶段 | 内容 | 触碰面 |
|---|---|---|
| P0 | 表 + REST + MCP 工具 | 纯新增，零风险 |
| P1 | context_builder 保送+加权（~60 行读路） | 注入质量核心收益 |
| P2 | local_filter 输出 topics + 存量回刷 | 写路 prompt 微调 |
| P3 | work-driver / HUD 接线 | 跨仓 |

## 十、留给 Claude Code 的评审点

1. 门槛问题 A/B（§六）；
2. match 两信号融合用 max 还是加权和？Q3 的 α=0 是否应设**负**（紧急不重要该压制？——含烟备注：压制放**行为轴**（work-driver 派活），不放**记忆轴**——记忆轴压制真相，行为轴才压拖延）；
3. 保送席会不会把真正高相关的 Q1 噪音顶进来（α 上限 + 相关性地板双闸）；
4. registry 与 `memory_conflicts` 表的关系要不要打通（新事实 supersede 旧优先级主题）；
5. topics 走 tags 命名空间 vs 顶层列（含烟倾向 tags：schema 零迁移）。

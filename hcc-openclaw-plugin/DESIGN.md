# hcc-memory 插件设计文档:Dreaming 渐进式实现 + Fallback

本文档记录 `hcc-memory` 插件两块能力的设计:dreaming(记忆巩固)如何对齐 OpenClaw
官方 memory-core 机制,以及 HCC 不可用时的健康探针 + 本地暂存 + 回灌 + 人格唤醒
fallback 方案。

## 一、官方 dreaming 机制摘要

OpenClaw memory-core 通过 `registerShortTermPromotionDreaming(api)` 注册一个 managed
cron job "Memory Dreaming Promotion",默认 `0 3 * * *`,以 isolated session 触发,
payload 是 system event token `__openclaw_memory_core_short_term_promotion_dream__`。
触发后对每个 workspace 跑 `runDreamingSweepPhases`,三阶段:

| 阶段 | 默认 cron | lookback | limit | 说明 |
|---|---|---|---|---|
| light | `0 */6 * * *` | 2 天 | 100 | 从 daily notes + session transcript 摘 snippet,dedupe 相似度 0.9,注入短期召回列表 |
| deep | (随 dreaming sweep) | — | 10(默认) | 见下 |
| rem | `0 5 * * 0` | 7 天 | 10 | minPatternStrength 0.75,从 memory/daily/deep 提炼模式写回 MEMORY.md |

**Deep 阶段**是我们对齐的重点:

- 候选来源:memory 文件 + daily + sessions + logs + recall 条目
- `rankShortTermPromotionCandidates` 用六分量评分:frequency / relevance / diversity /
  recency / consolidation / conceptual,加权得 score
- 默认门槛:`limit=10, minScore=0.8, minRecallCount=3, minUniqueQueries=3,
  recencyHalfLifeDays=14, maxAgeDays=30`
- `applyShortTermPromotions`:达标的 snippet 写入 MEMORY.md 的 `## Durable Memories`
  区,带 managed marker(如 `<!-- openclaw:memory:promoted -->`)和召回统计注释
- `writeDeepDreamingReport`:写 DREAMS.md(`storage.mode="separate"` 时在
  `memory/.dreams/DREAMS.md`;`inline` 时嵌进 MEMORY.md 的 `## Dream Report` 块)
- 梦境叙事:用 subagent 基于 candidates/promotions 生成可读梦境日记,脱敏(
  `REM_REFLECTION_TAG_BLACKLIST = assistant/user/system/subagent/the`),写入 DREAMS.md
  或单独梦境文件
- 失败时 `appendFailedDreamingEvent` 记录错误

## 二、我们的对齐点:评分近似映射表

官方六分量依赖 embedding 相似度、召回统计(recall count、unique queries)等 OpenClaw
workspace 才有的数据。HCC 目前没有这些维度,只有 `importance / access_count /
last_access / created_at / tags`。映射如下(实现见 `lib/dreaming.js`
`scoreMemory()`):

| 官方分量 | 我们的近似 | 计算方式 |
|---|---|---|
| recency | recency | `2^(-ageDays/halfLifeDays)`,ageDays 取 `last_access ?? created_at` 到现在的天数,halfLifeDays 默认 14(与官方一致) |
| frequency | frequency | `min(access_count / freqNormCap, 1)`,freqNormCap 默认 10 |
| consolidation | consolidation | 直接用 `importance`(已经是 0~1) |
| diversity | diversity | `min(tags.length / diversityNormCap, 1)`,diversityNormCap 默认 5 |
| relevance | (未实现) | 官方靠 embedding 相似度衡量候选与已有记忆的相关性,HCC 侧没有本地可用的 embedding 比对,暂不做这一分量 |
| conceptual | (合并进 diversity) | 官方的概念多样性和我们的 tag 多样性目的相近,直接复用同一个 diversity 分量,不单独建模 |

默认权重 `{recency:0.3, frequency:0.25, consolidation:0.25, diversity:0.2}`。

门槛对照:

| 门槛 | 官方默认 | 我们的默认 | 原因 |
|---|---|---|---|
| minScore | 0.8 | 0.8 | 保持一致,但因为我们少了 relevance 分量,实际达标会比官方难——这是有意保守,见"已知取舍" |
| minRecallCount / minAccessCount | 3 | 3 | 用 `access_count` 直接对应 `recallCount`,HCC 的 `/memory/touch` 语义(检索命中即 access_count+1)与官方 recall 计数目的一致 |
| minUniqueQueries | 3 | (未实现) | HCC 没有记录每次检索用的 query,无法还原 unique queries,用 diversity(tag 多样性)间接近似 |
| recencyHalfLifeDays | 14 | 14 | 一致 |
| maxAgeDays | 30 | 30 | 一致 |
| limit | 10 | 10 | 一致 |

**为什么用 tag(`promoted:deep`)而不是专用字段做提升标记**:HCC 的
`MemoryUpdate` schema 只有 `content/summary/importance/tags/status/embedding`,没有
"是否已被 dreaming 提升过"这种专用布尔字段,加字段要改 HCC 数据库 schema(任务约束
不改 HCC 代码)。用 tag 是纯客户端可控的做法,幂等检查只需要读 `tags` 数组,不用改
后端。代价是 tag 语义会和用户自己打的 tag 混在一起,只要约定 `promoted:deep` 前缀不
被用户占用就没有冲突风险。

## 三、Dreaming 触发方式

官方靠 managed cron job + isolated session。我们目前没有直接挂进 OpenClaw cron 的
接口(需要在 openclaw.json 里配置,任务约束不改这个文件),所以采用两条并存的路径:

1. **`memory_dreaming` 工具**(参数 `{phase: "deep"}`):可以被 OpenClaw 的 cron 配置
   调用(未来在 openclaw.json 里配一条 cron,payload 调这个工具),也可以被用户/agent
   手动调用。
2. **插件内部 `setInterval`**:`register()` 时如果 `dreaming.enabled` 且
   `dreaming.intervalMs > 0`(默认 6h = 21600000ms),启动一个定时器直接调
   `PHASES.deep.run()`。

两者并存是因为:方式 1 依赖用户后续在 openclaw.json 里配置 cron(这一步不在本任务
范围内,由另一实验步骤负责),如果一直不配,`setInterval` 保证 dreaming 仍然会跑;
等 cron 配置好之后,两者会同时存在(重复触发的影响是幂等的——tag 检查保证不会重复
提升同一条记忆,只是多花一次 HCC 请求),用户可以按需把 `intervalMs` 设为 0 关掉
`setInterval`,只用 cron。

**已知取舍**:插件进程的生命周期由 OpenClaw gateway 管理,`setInterval` 只在 gateway
进程存活期间有效——gateway 重启会清空定时器状态(不是持久化的 cron),下一个 6 小时
窗口从 gateway 重启那一刻重新计时。如果需要精确的"每天固定时间跑"语义,必须走
`memory_dreaming` 工具 + OpenClaw 官方 cron,而不是这个 `setInterval`。

## 四、阶段计划

- **已实现:deep**。见 `lib/dreaming.js`。
- **light 阶段暂不实现**,原因:官方 light 阶段的数据源是"当日 daily note"和
  "session transcript",这两者在 OpenClaw workspace 里的具体路径/格式我们还没有
  确认过对接方式(是走 workspace 目录里的 md 文件,还是走 HCC 的 `/memory/recent`
  短窗口取近似),需要用户决定数据源之后再实现,避免猜错摄入路径导致产出内容
  文不对题。
- **rem 阶段暂不实现**,原因:官方 rem 阶段依赖 subagent 调用生成脱敏叙事,这涉及
  模型调用的频率/成本决策(多久跑一次、用什么模型、失败重试策略),需要用户先拍板
  之后再接。`lib/dreaming-phases.js` 里已经留好 `PHASES.rem` 的接口占位。

## 五、Fallback 设计

### 健康探针(`lib/health.js`)

`createHealthProbe({baseUrl, intervalMs=30000, timeoutMs=5000, failureThreshold=3})`
独立轮询 `/api/v1/health`,不依赖业务请求的成败来判断健康——这是为了避免"半死"
误判:HCC 慢但没挂时,如果拿业务请求的一次超时就判 unhealthy,会造成过度降级。探针
自己的超时阈值是 5s,连续失败 3 次才判 unhealthy;从 unhealthy 恢复到 healthy 只要
一次探测成功。探针失败不抛异常,只记录日志。

**已知取舍**:启动时 `healthy` 状态乐观初始化为 `true`,交给第一次探测去修正——如果
HCC 在插件刚启动时就已经挂了,要等 `intervalMs * failureThreshold`(默认最多 90s+)
才会被探针判定为 unhealthy,这段时间内的业务请求会先尝试 HCC、失败后各自 catch 到
本地 fallback(`withHccOrFallback` 里 hccCall 失败会 fallback,不会真的丢数据),只是
不会一开始就走"抢跑"式的直接 fallback。

### 本地存储(`lib/local-store.js`)

优先用 Node 内置 `node:sqlite`(`DatabaseSync`,Node 22.5+ 自带,标记为实验特性但已
在本机 Node v22.23.1 验证可用)。如果运行插件的 Node 版本没有 `node:sqlite`,自动退
回纯 JSON 文件存储(`JsonStore`),接口保持一致,调用方不用关心具体后端。存储路径固
定在 `~/.openclaw/hcc-memory/fallback/`(不做成配置项,这是插件私有状态目录)。

两张表:
- `staged_writes`:HCC 不可用期间暂存的待回灌写入
- `local_memories`:fallback 期间可检索的本地记忆快照(`stageWrite` 时会同步写一份
  到这张表,所以暂存的内容立刻可被 `searchLocal` 检索到,不用等回灌成功)

`searchLocal` 用简单的关键词子串匹配 + 命中数排序,不依赖 embedding。

### 容错改造(`index.js`)

所有对 HCC 的读写都走 `withHccOrFallback({hccCall, fallbackCall})`:探针 healthy 才
尝试 `hccCall`;探针 unhealthy,或者 `hccCall` 抛错,都落到 `fallbackCall`。

- `memory_search` / `memory_get` 降级时返回 `degraded: true, backend: "local"`,
  并且尝试读 `含烟人格/SOUL.md`(截断 500 字符)注入 `context` 字段做"本地人格
  唤醒"提示;SOUL.md 不存在就跳过,只提示"未找到本地人格文件"。
- `session_end` / `before_compaction` / `tool_result_persist` 写入降级时走
  `stageWrite`,`source` 字段追加 `_fallback` 后缀,不丢数据。
- 探针 unhealthy→healthy 翻转时,`health.onRecovered()` 自动触发
  `scripts/backfill.js` 的 `runBackfill()`,异步执行,不阻塞探针本身。

### 回灌(`scripts/backfill.js`)

遍历 `staged_writes` 里 `synced=0` 的条目,回灌前先用内容前 100 字符查一次
`/memory/search` 做幂等去重(找到完全匹配 content 的已存在记录就只标记 synced,不
重复写入),否则 `POST /memory/store`。成功 `markSynced`;失败保留,下次重试。

## 六、验证结果摘要

- `node --check` 全部文件通过(index.js + 7 个 lib 文件 + 3 个 script 文件)
- 模块加载验证:`lib/dreaming.js`、`lib/dreaming-phases.js`、`lib/health.js`、
  `lib/local-store.js`、`lib/config.js`、`index.js` 均可正常 `import()`
- `scripts/test-fallback.js`:健康探针在连续 3 次失败后正确判定 `healthy=false`;
  本地存储 stage/list/search/markSynced 全部通过;回灌真实 HCC 成功写入一条带
  `FALLBACK_TEST_<timestamp>` 标记的测试记忆,`/memory/search` 确认存在后通过
  `/memory/delete` 清理,清理后确认搜索不到——未污染 HCC 数据
- `scripts/run-dreaming.js deep`(真实调用 HCC,`baseUrl=http://100.66.103.69:8000`):
  扫描 178 条记忆,0 条达标(HCC 现有记忆里 `access_count` 普遍偏低,不满足
  `minAccessCount=3` 门槛),按要求**未降低门槛、未伪造数据**,如实在 DREAMS.md /
  AICore 梦境日记里写"门槛未达标,未提升任何记忆" / "今夜无梦"
- 完整执行输出见 `/Users/michael/workspace/projects/HCC/.task-logs/task1-exec.log`

## 七、待用户决策

1. **light 阶段数据源**:走 OpenClaw workspace 里的 daily note/session transcript
   文件,还是用 HCC `/memory/recent` 的短窗口近似?前者更贴近官方语义但需要确认
   具体路径约定,后者实现简单但语义有偏差。
2. **rem 阶段的 subagent 调用**:多久跑一次(建议不要比 deep 更频繁,成本考虑)、
   用哪个模型、生成失败要不要重试、脱敏规则要不要在 `REM_REFLECTION_TAG_BLACKLIST`
   基础上加中文黑名单词。
3. **`memory_dreaming` 工具是否要接进 OpenClaw 官方 cron**(在 openclaw.json 里配置
   一条定时任务调用它)——本任务约束不修改 openclaw.json,只是把工具和 `setInterval`
   都准备好了,接不接 cron、cron 表达式定多少由用户决定。
4. **deep 阶段 `minScore=0.8` 在真实数据下几乎不可能达标**(因为 HCC 现有记忆的
   `access_count` 普遍是 0,`minAccessCount=3` 这一硬门槛就先把大多数记忆挡在外
   面)。是否要:(a) 保持现状,等 `/memory/touch` 被检索路径更频繁调用后数据自然
   积累;(b) 降低 `minAccessCount`/`minScore` 默认值;(c) 给 frequency 分量换一种
   不依赖 `access_count` 的近似(比如结合 `updated_at` 变化次数)。这个改动会直接
   影响哪些记忆被"巩固",不应该由我方臆断,留给用户选择。
5. **fallback 期间的 `local_memories` 表要不要设置容量上限/过期清理**——目前没有
   限制,长期不回灌会无限增长;是否需要加一个 TTL 或最大条数限制,取决于用户对
   "HCC 长期不可用"场景的容忍度预期。

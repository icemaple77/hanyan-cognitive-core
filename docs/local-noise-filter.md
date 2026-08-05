# HCC 本地模型记忆降噪设计

> 状态：设计稿，未实现。不改动任何运行配置/生产数据——本文档所有测试均直连本机 Ollama（`http://localhost:11434`），未写入 HCC 数据库。
> 关联：`core/orchestrator.py` · `core/event_bus.py` · `core/emotion_events.py`（同款订阅模式，本方案直接复用）· `gateway/services/__init__.py`（现有检索侧降噪）· `gateway/core/embeddings.py`（现有 Ollama 调用范式）

## 零、现状评估

代码审计结果（不是猜测——逐文件读过）：

1. **`core/orchestrator.py` 的 `evaluate()` 是纯规则引擎，从未调用任何 LLM——云端、本地都没有。** 关键词表（`LOW_VALUE_PATTERNS`/`HIGH_IMPORTANCE_KEYWORDS`/`HIGH_VALUE_TOPICS`）+ 长度阈值 + 字符 bigram 去重，全部是字符串匹配。这和最初"云端 LLM 太贵/太慢所以要换本地"的假设不一样：真正的问题是**规则引擎没有语义理解**——像"我最喜欢秋天"这种没命中任何关键词的隐性偏好表达会被判为不存，而"文件原文摘录"这种命中了"配置"之类关键词的纯噪音会被误判为高价值。
2. **`/orchestrator/evaluate`（`gateway/api/cognitive_routes.py:31`）是独立的、可选调用的端点，不是写入路径上的强制关卡。**
3. **`/memory/store` → `MemoryService.create()`（`gateway/services/__init__.py:29`）直接落库，代码里完全没有调用 `evaluate()`。** 不止 OpenClaw 插件绕过了它——任何客户端都可以绕过，包括 Hermes 的 hcc provider：它能过滤 hi/hello 是因为**客户端自己**先调 `/orchestrator/evaluate` 再决定要不要调 `/memory/store`，这是调用方的自觉行为，不是 HCC 强制的。
4. **OpenClaw 插件**（`~/hcc-openclaw-plugin`，不在本仓库）的 `tool_result_persist` hook 直接 POST `/memory/store`，硬编码 `type=tool_result, importance=0.3, source=openclaw_plugin`，从不碰 `evaluate`。实测抓取的样本证实了这一点（见第一节）。
5. **已有检索侧降噪**（`gateway/services/__init__.py:15-22`，`NOISE_TYPE`/`NOISE_IMPORTANCE_THRESHOLD=0.5`）只在查询时排除 `importance<0.5` 的 `tool_result`，只治检索：存量仍在库里占索引/存储成本，且"被自动过滤但其实有价值的 tool_result"和"纯垃圾"共用同一静态阈值，没有真正甄别，只是搬到了统一的默认值 0.3 之下。
6. **`core/event_bus.py`（上一 commit `808f1af` 刚接入）+ `core/emotion_events.py` 已经打好了本方案要用的地基**：`/memory/store` 成功后会发布 `MEMORY_CREATED` 事件（`gateway/core/events.py:publish_memory_event`），`emotion_events.py` 示范了标准写法——"gateway lifespan 里订阅 → 异步回调 → 失败吞掉、绝不阻塞原请求"。本方案直接照抄这个模式，不用重新发明挂载点。
7. **顺带发现**：`core/model_router.py` 的 `DEFAULT_MODELS`/`HARDWARE_PROFILES` 给 memory/emotion 配了 `qwen3:8b`，dream 配了 `qwen3:14b`——但本机 `ollama list` 里根本没有这两个 tag（实际是 `qwen3.5:0.8b/2b/4b/9b` 系列，命名不同）。而且全仓库搜不到任何真正的 chat/completion 调用点（`dream_narrative.py` 是纯模板渲染，不调模型）——`model_router.py` 目前是完全没有消费者的配置脚手架。**本方案很可能是 HCC 第一个真正落地的本地 LLM 调用方**，第七节会顺手把这个配置对齐一下。

## 一、可行性验证（已本地实测，未改动任何生产配置）

抓了 HCC 库里的真实样本（3 条 OpenClaw `tool_result` 噪音 + 2 条 Hermes 写入的真实对话记忆）扩展成 8 条测试集，直连本机 Ollama 跑了四个候选模型：

| 模型 | 通过率 | 平均耗时/条 | 备注 |
|---|---|---|---|
| qwen3.5:0.8b | 3/8 | 0.74s | 太不稳定，把大部分内容（含纯噪音）判为 keep=true |
| qwen3.5:2b | 4/8 | 1.16s | 反过来又太保守，把真实决策类内容也判 false |
| **qwen3.5:4b** | **7/8** | **1.76s** | 唯一漏判：无关键词的隐性偏好句"我最喜欢秋天，尤其是桂花开的时候" |
| qwen3.5:9b | 7/8 | 2.88s | 准确率和 4b 打平，只是更慢，不值得 |

**关键前提约束（不满足这条，方案跑不通）**：qwen3.5 系列默认是"思考模型"，不显式关闭思考，模型会把整个 `num_predict` 输出预算全部花在 `<thinking>` 推理链上，最终 `response` 字段是空的，永远吐不出 JSON（第一轮测试全军覆没就是踩了这个坑）。Ollama API 需要显式传 `"think": false`，加上之后同样的调用从"输出为空"变成 0.86s 出正确 JSON。

样本量小（8 条），这只是方向性验证，不是严格基准——第七节 P0 步骤里安排了大样本（100-200 条人工标注）复测，正式定阈值前必须做。

## 二、模型选型：qwen3.5:4b

- 准确率与 9b 打平，速度快约 40%（1.76s vs 2.88s），本地批量清理 2413 条时这个差距会被放大成十几分钟的实际差异。
- 显存/内存占用约 3.2GB，Mac Mini M4 上和其他模型（`qwen3.5:9b`、`qwen2.5-coder:14b` 等）共存不冲突。
- 冷启动（模型换出后首次调用）约 3-4s，暖启动（模型已驻留显存）约 1.3-1.5s——只要调用频率不低到让 Ollama 把模型换出，实际大部分调用都是暖启动延迟。
- 0.8b/2b 体量太小，在"识别隐性价值/隐性噪音"这类需要语义理解而非关键词匹配的任务上不可靠——这恰恰是本方案要解决的问题（现有规则引擎的短板就是语义理解），选一个连语义理解都不稳定的模型没有意义。

## 三、提示词模板（已实测）

```text
你是记忆库的存储质量守门员。判断下面这条内容是否值得作为长期记忆保存。

不值得存(keep=false)的例子：
- 工具调用的原始返回结果(文件读取内容、命令行输出、API 原始 JSON、会话列表/历史转储)
- 空结果或无信息量的状态确认("操作成功"、"messages: []")
- 日常寒暄、无实质内容的短句

值得存(keep=true)的例子：
- 用户的决定、偏好、事实性信息("我喜欢...","我们决定...")
- 项目进展、架构决策、bug修复方案、重要配置
- 情感表达、重要事件、承诺

只输出一行 JSON，不要任何解释：{"keep": true或false, "importance": 0到1之间的浮点数}

内容：
<<<{content}>>>
```

调用参数（已验证有效）：`think: false`、`format: "json"`、`temperature: 0`、`num_predict: 60`。

已知短板：隐性偏好表达（没有"喜欢/决定"这类显式关键词，但对陪伴场景重要）容易漏判。P0 大样本验证阶段建议在 few-shot 例子里再加 1-2 条这类样本，参考 `core/orchestrator.py` 里已经总结好的 `COMPANION_IMPORTANCE_KEYWORDS`（"想你"、"纪念日"、"喜欢的季节"等）反哺进提示词。

内容截断：测试用了 1500 字符截断。真实 `tool_result` 样本里见过嵌套到几千字的 JSON 转储（工具结果里递归引用了之前的工具结果），截断可能丢失判断所需的关键信息——大样本验证阶段要专门测不同截断长度对准确率的影响，不能直接沿用 1500 这个测试期间随手定的数字。

## 四、调用链路设计：HCC 侧统一，异步、不阻塞写入

**不做同步阻塞式改造**——把本地模型调用直接塞进 `/memory/store` 会给所有写入方（包括 Hermes 实时对话）增加 1.5s+ 延迟，用户能感知到，不可接受。

设计为两层：

- **L0（保留）**：`core/orchestrator.py` 现有规则引擎，<1ms，零成本，继续挡掉最明显的噪音（比如把已确认的噪音句式，如"Successfully replaced N block(s)"、`"messages": []"`，补进 `LOW_VALUE_PATTERNS`，见第七节 P1）。
- **L1（新增）**：`core/noise_filter.py::local_evaluate(content, source, type) -> {keep, importance}`，用 `httpx` 调本机 Ollama，写法照抄 `gateway/core/embeddings.py::_embed_ollama`（env 变量配置 + try/except 回退）。

挂载点复用 EventBus，完全照抄 `core/emotion_events.py` 的订阅模式，新增 `core/noise_filter_events.py`：

```python
async def _on_memory_created(event: Event) -> None:
    if not _is_low_trust(event.payload):   # type=="tool_result" or source in {"openclaw_plugin", ...}
        return                              # 高信任来源不重复跑模型，省算力
    decision = await local_evaluate(...)
    if not decision.keep:
        await _mark_discarded(event.payload["memory_id"])   # 软删，status="discarded"
    else:
        await _update_importance(event.payload["memory_id"], decision.importance)

async def subscribe_noise_filter_events() -> None:
    bus = get_event_bus()
    await bus.connect()
    await bus.subscribe([EventType.MEMORY_CREATED], _on_memory_created)
```

在 `gateway/main.py` 的 `lifespan()` 里跟 `subscribe_emotion_events()` 挂在一起启动。

需要的唯一现有代码改动：`gateway/api/memory_routes.py:36` 的 `publish_memory_event` 调用目前只传了 `user_id/content/importance/tags`，缺 `type`/`source`——要补上这两个字段，`_on_memory_created` 才能判断"这是不是低信任来源"。

效果：不管是 Hermes、OpenClaw 插件、`scripts/sync_openclaw_memory.py`，还是未来任何新客户端，只要走 `/memory/store`，都会自动触发这层异步复核——**不需要改任何客户端/插件代码**，这是"HCC 侧统一"相对"插件侧改造"的核心优势：插件在另一个仓库、由 OpenClaw 那边维护，HCC 这边管不了它什么时候更新。

失败/超时处理：`local_evaluate` 异常时直接跳过复核、保留调用方原始 `importance`——不回退到 L0 规则引擎重新跑一遍，因为这条内容本来就是 L0 没识别出问题（否则一开始就不会走到这一步）；跳过好过误判。

## 五、性能与批处理设计

- 单条：暖机 1.3-1.5s（4b, think:false, num_predict 60），冷启动（模型被换出后首次）+3s 左右。
- 异步事件驱动，用户侧感知延迟为 0——复核结果在写入后 1-2 秒内"悄悄"生效（`importance` 变化或软删除）。
- 并发：实测 4 并发下等效吞吐约 0.8s/条（4 次调用墙钟耗时 3.1s，对比顺序 4 次 6.8s），约 2.2x 加速；本机 Ollama 默认并发度能扛住这个量级。批量清理场景（第六节）会用到。
- 缓存：OpenClaw 的工具日志里有大量结构相同、只有路径/id/时间戳不同的内容（比如反复出现的 `"messages": []` 空历史、`"Successfully replaced N block(s)"` 确认句）。与其做语义缓存，不如直接把这类**已确认的固定噪音句式**升级进 L0 规则层（`LOW_VALUE_PATTERNS`）——零成本命中，连 L1 都不用跑。真正需要跑模型的应该只是规则层判断不了的"灰色地带"内容，长期看能跑模型的比例会随着规则表积累而下降。

## 六、存量清理设计（当前 2413 条）

独立脚本 `scripts/noise_filter_backfill.py`（风格参考已有的 `scripts/sync_openclaw_memory.py`：一次性/可重跑工具，stdlib+httpx 即可跑，不依赖 HCC venv）：

- 分页拉取 `/memory/search`（先只扫 `type=tool_result`，噪音源头已知集中在这里；要不要扩大到全库需要用户单独确认，不在本方案默认范围内）。
- `--dry-run` 模式：只统计分布（keep=false 占比、importance 分布直方图），不改库——先看分布再决定阈值和是否真正执行，这是硬性前置步骤，不能跳过。
- 断点续跑：不用额外状态文件，复核过的记录打 tag `noise_filter_v1:done`，重跑时按 tag 过滤跳过已处理的（`sync_openclaw_memory.py` 用的也是这个"用 tag 做幂等标记"思路）。
- 并发 4（复用第五节实测），2413 条按等效吞吐 0.8s/条估算约 32 分钟——这是全库上限，实际 `tool_result` 子集应该明显小于 2413，跑之前脚本会先查一次确切数量再给准确预估。
- 执行模式：`keep=false` → `status="discarded"`（软删，不物理删，可回滚）；`keep=true` → 用模型评分覆盖硬编码的 `importance=0.3`，让检索侧现有的 `NOISE_IMPORTANCE_THRESHOLD=0.5` 判断对这批"复核后其实有价值"的记录生效，重新变得可搜索。

## 七、实施步骤

**P0（最小可行，本次不执行，等用户确认后再动手）**

1. `core/noise_filter.py`：`local_evaluate()`，httpx 调 Ollama，`think:false` + `format:json`，异常/超时直接跳过复核（不回退 L0）。
2. `gateway/api/memory_routes.py`：`publish_memory_event` 补 `type`/`source` 两个 payload 字段（一行改动）。
3. `core/noise_filter_events.py`（仿 `emotion_events.py`）+ `gateway/main.py` lifespan 里挂订阅。
4. 大样本人工标注验证（100-200 条真实 `tool_result` + 真实对话记忆混合），重新跑一遍 4b/9b 对比，用真实分布锁定最终 prompt 措辞和判断阈值——8 条测试集只够定方向，不够定生产参数。
5. `scripts/noise_filter_backfill.py --dry-run`，先看存量分布，不落地任何改动。

**P1（存量清理执行 + 优化，P0 验证通过后再排期）**

6. `--dry-run` 分布确认合理后，先小批量抽样人工抽查复核结果，再全量执行存量清理。
7. 规则层扩容：把 P0 阶段确认的固定噪音句式（"Successfully replaced"、`"messages": []"` 等结构化模式）补进 `orchestrator.py` 的 `LOW_VALUE_PATTERNS`，减少需要跑模型的比例。
8. `model_router.py` 里补一个 `noise_filter` 模块配置项，顺手把 memory/dream 模块配置的模型 tag 和本机 `ollama list` 实际存在的 tag 对齐（第零节第 7 点提到的历史遗留问题）。

## 八、风险

- **EventBus 目前是进程内 broker**（`HCC_REDIS_ENABLED=false` 默认值，且 `Dockerfile` 的 `CMD` 是单个 `uvicorn` 进程、没有 `--workers`，所以现状下没有跨进程问题）。这个前提要记住：如果以后网关改成多 worker 部署，一个 worker 收到写入请求发布事件，另一个 worker 的订阅者收不到（in-memory broker 不跨进程）——这是继承自 `emotion_events.py` 的既有限制，不是本方案新增的，但要跟着一起切 `HCC_REDIS_ENABLED=true`。
- **本地模型判断不是 100% 准确**（4b 在 8 条测试里也有 1 条误判），所以"软删除"（`status=discarded` 而非硬删除）是必需的安全网，不是可选项——误判要能人工找回。
- **8 条小样本测试不能代表真实分布**，2413 条存量内容形状远比测试集丰富（中英混排、深层嵌套 JSON、超长内容），P0 第 4 步的大样本验证是必须的，不能跳过直接拿这份文档里的阈值上生产。
- **Ollama 资源争抢**：如果 `model_router.py` 配置的 emotion/dream 模块以后真的开始被调用（目前代码审计未发现调用点，本方案可能是第一个真实消费者），会和本方案共享同一个本机 Ollama 实例的算力。批量存量清理建议挑本机空闲时段跑，正式上线前应确认这一点是否还成立。

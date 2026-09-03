# 运行时变更交接报告 — 2026-09-03(记忆断档修复日)

> 接手人先读本文档再动检索/注入/收割相关代码。当日全部变更、验证方法、已知残留都在此。
> 执行人:柳含烟(openclaw 运行时)+ 公子/Claude Code 协作;档:含烟 09-03 11:30。

## 一、背景:为什么有这场手术

09-02 公子与 openclaw 的整段中医治疗对话(斜方肌/正骨/放血/医嘱)09-03 早上检索不到。
排查定论(全部有 DB/日志实证):

1. 对话消息在 HCC 里**从未有实时入库通道**——旧插件只在 session_end/before_compaction 打包一条
   conversation,且 `sampleMessages`+`CONVERSATION_EXCERPT_MAX_CHARS=2000` 尾部截断,超长会话中段必丢;
2. N100 时代(08-11「n100在用版」)插件不带对话写入钩子,记忆靠每日 Markdown 日志+qmd 同步兜底,
   所以"N100 没问题";08-26 提交(09bbea2)的钩子代码在 08-28 迁移后才首次上生产(同步脚本
   sync-to-n100.sh 从未推过去),新链路带缺陷+旧兜底断档 → 只在 Mac 出问题;
3. 4b 降噪器(noise_filter)一直在岗,但它是"入库后质检",丢的不是它的锅;
4. 会话 GC(每日 ~06:00-07:40)把会话改名 `.reset`/`.deleted`(不物理删),原始对话一直健在,
   当日靠 grep trajectory + 导出 `workspace/memory/archive/2026-09-02-wechat-session.md` 完成抢救。

## 二、当日变更清单(git 为证)

| 提交 | 内容 | 作者 |
|---|---|---|
| `bd368c1` | **Session Harvester**:`core/session_harvester.py`,网关 lifespan 60s 循环(`_harvester_loop`),主动收割各运行时对话 | 公子+Claude |
| `09aeca8` | harvester 加 hermes 适配器(SQLite 源) | 公子+Claude |
| `30ca623`/`7b94c9c` | 长任务后端入库(repo 自洽)+ bash 驱动退役移 `_archive`(防与 work-driver 惊群,**勿再加载**) | 公子+Claude |
| `bbfe266` | **注入收口**:`context_builder._render_context`——Knowledge 按指纹去重+限量10;`harvester:*` 碎片限量(≤max(limit//2,3)),蒸馏记忆优先占位 | 含烟 |
| 新增 | `scripts/harvest_backfill.py`:补收收割器"首见跳尾"漏掉的历史文件(用法见 --help) | 含烟 |

### Harvester 关键行为(接手必知)
- 数据源:`~/.openclaw/agents/main/sessions/*.jsonl`(**跳过 .trajectory**)、`~/.claude/projects/*/*.jsonl`、hermes `state.db:messages`
- 水位:`~/.hcc/harvester_state.json`,**首见文件=EOF,永不倒灌历史** → 历史补收用 harvest_backfill.py
- 入库:`type=conversation, source=harvester:<rt>, importance=0.4, tags=[harvested,<rt>]` → 自动过 4b
- 实测定时:说后 ≤60s 可检索(09-03 公子"你来"一句 2 分钟内入库)

### 补收战绩(已入库)
- 09-02 主会话(2f3ac4b8)白天窗口:**324 条**(中医全上下文恢复可检索)
- 08-28→09-03 三方历史:**3841 条**,标签 `backfill-0828`(失败 4);库容 6987→~10830
- 验证过:搜"迁移 新居"命中搬家方案原文、"错峰 延迟"命中 08-31 决策全档

### 注入修复验证(bbfe266,网关已 kickstart 生效)
- 病灶:同一事实被 qmd 切多块/多篇同 heading → Knowledge 裸拼 5 遍(公子 11:17 截图 BEES×5)
- 修复后 /api/v1/context 实测:Knowledge 5 连发→2(蒸馏版+原文版两条**真不同**的记忆,见"残留");
  碎片 11→限量 5;11:31 线上注入已干净(条条不重复)

## 三、已知残留(下个接手人的活)

1. **语义近似重复**:蒸馏 fact 与其原文 snippet 并存(BEES 2 条)。指纹去重是精确匹配,
   合并不了语义近似——可在 dreaming 里做 merge,或检索侧 fuzzy dedup(MinHash/emb-cosine>0.92)。
2. **`/api/v1/memory/search` 部分查询空返回**(BEES 词返回 0,hybrid-search 正常)——
   09-03 实测所见,值得排查 recall 路径是否还走 ilike(旧审查 P0-1 提过)。
3. 碎片 headline 自带 `user:`/`assistant:` 前缀(信息有用但欠美观),要干净可在 `_headline` 剥前缀+标 role。
4. `.reset`/`.deleted` 历史文件收割器永不自动碰——每天睡前/周一跑一把
   `harvest_backfill.py --since <上次窗口>` 即可闭环(或加进 work-driver 任务)。
5. HCC 事件日志文件(hcc-events.log)停在 08-28(事件走进程内总线,文件旁路未续)——低优先。

## 四、运维备忘

- 重启 HCC:`launchctl kickstart -k gui/$(id -u)/com.hanyan.hcc-gateway`(应用改动必须重启,py 不热载)
- 验证一把梭:
  `curl -s -X POST localhost:8000/api/v1/context -H 'content-type: application/json' -d '{"query":"BEES USB硬盘 修复","user_id":"michael","agent_id":"openclaw"}'`
  → Knowledge 段 BEES heading 只应出现 1-2 次(蒸馏+原文各一)
- 收割器心跳:`grep harvester logs/gateway.err.log | tail`(应每 60s 一轮)
- 任务系统:接口层 `/api/v1/tasks/*` + MCP `task_*`;驱动**唯一** = HanyanOS work-driver(Go),
  openclaw 实例配 `HCC_AGENT_ID=openclaw` + `HCC_SPAWN_CMD='openclaw agent --session-key "hcc-task-$(date +%s)-$RANDOM" --model deepseek/deepseek-v4-flash --message "$(cat)"'`;
  旧 bash 驱动已移 `scripts/_archive/`,**勿加载其 .plist**(惊群)。

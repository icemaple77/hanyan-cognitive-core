# N100 金丝雀演练报告 — OpenClaw 2026.8.2

> 日期：2026-09-03 ｜ 执行：柳含烟（公子授权演练，主机更新未动）
> 结论：**可以升，但必须先做 4 项预备**。N100 演练全程零生产污染。

## 一、演练结果总览

| 项目 | 结果 |
|:-----|:----:|
| `openclaw update --no-restart` 7.1-2 → 8.2 | ✅ 121s，无报错 |
| 网关冷启动（配置齐全后） | ✅ 4.4s ready，13 插件 |
| hcc-memory 插件加载 | ✅ 但需两处新配置（见下） |
| 微信/飞书抢连风险 | ✅ 全程 disabled，零进程 |
| 生产 HCC 污染 | ✅ baseUrl 隔离到 127.0.0.1:9，零字节 |

## 二、🔴 关键发现：会话存储搬家（影响 harvester）

- 8.x 启动时把 `agents/*/sessions/*.jsonl` **全部迁入 per-agent SQLite**：
  - 新位置：`~/.openclaw/agents/main/agent/openclaw-agent.sqlite`
  - 核心表：`transcript_events(session_id, seq, event_json, created_at)` —— event_json 就是原 jsonl 行（type=session/message/...）
  - 辅助表：`session_transcript_active_events(session_id, active_position, event_seq, message_position, context_eligible)`、`session_nodes`、`conversations`、`session_conversations`
  - 原 jsonl → `session-sqlite-import-archive/`，后缀 `.imported-<ts>`（N100：1754 件，83M）
  - sessions 目录只剩 `.trajectory-path.json` 碎片
- **后果**：openclaw harvester 的 `*.jsonl` glob 更新后全部失明 → **更新前必须先写 SQLite 适配器**（复用 hermes 模式，水位=created_at/seq）
- Mac 更新后旧 jsonl 同样进归档——**归档不删**，harvester 老逻辑保留作回退

## 三、8.x 新硬闸（Mac 更新清单必做）

1. **capability consent**：更新后逐个执行
   `openclaw plugins enable hcc-memory --accept-capabilities`（codex 同理）
   未授权时 doctor 只报 "replacement deferred" 警告，但网关启动校验可能拒启
2. **allowConversationAccess**（实测抓获）：
   `[plugins] typed hook "before_prompt_build" blocked because non-bundled plugins must set plugins.entries.hcc-memory.hooks.allowConversationAccess=true`
   → 不加 = **HCC 上下文注入静默失效**（memory 回忆还在，注入没了）
   → 配置：`plugins.entries.hcc-memory.hooks: {"allowConversationAccess": true}`，实测加后 blocked=0
3. **secret 严格化**：`gateway.auth.token` 引用 env `OPENCLAW_GATEWAY_TOKEN`，冷启动解析不到 = **Startup failed: required secrets are unavailable**（7.x 是降级容忍）。provider key 缺失仅 SECRETS_DEGRADED 警告+用到才败。Mac launchd 环境更新前须验证全部 secret 引用可解析
4. **tailscale 路由归属校验**：stale 443 路由（旧版遗留）→ 拒启。解锁命令在报错里：`tailscale serve --yes --https=443 --set-path=/ off`。Mac 更新前先 `tailscale serve status` 拍照
5. 彩蛋：`volcengine` 内置→外部按需装 ✅（`plugins install @openclaw/volcengine-provider --accept-capabilities`）；`memory-core` 上线即自建 "managed dreaming cron"——**更新后要检查它与 HCC dreaming 是否重复触发**

## 四、doctor 备注

- `update` 内置 doctor 因 masked 服务报 "could not enter maintenance" 未跑完 → 8.x 的 `doctor --fix`/`update repair` 需要能控制服务的环境；masked 机器上只能手动处理（Mac 是 launchd 服务，预期同样卡——预留手动预案：跳过 doctor，手工做第 3 节的 4 项）
- `codex/gpt-5.5` 迁移：doctor 未完成，Mac 更新时单独验证

## 五、N100 现状（演练后）

- 版本：2026.8.2 (0965053)，服务保持 masked/inactive，无残留进程
- 隔离保持：weixin/feishu `enabled:false`（entries+channels 双层）、hcc baseUrl=127.0.0.1:9、tailscale mode=off
- 原始配置备份：`~/.openclaw/openclaw.json.pre-canary-20260903-1218`（恢复时注意：里面通道是开的，恢复前须先想清楚抢连风险）
- 观察期：金丝雀静置 24-48h，期间 Mac 不动

## 六、主机（Mac）更新前置 checklist

- [ ] harvester 加 openclaw-agent.sqlite 适配器并本地验证（含新旧双读）
- [ ] 备份：openclaw.json + state/openclaw.sqlite + agents/*/agent/*.sqlite + sessions 目录快照
- [ ] openclaw.json 预写：hcc-memory.hooks.allowConversationAccess=true
- [ ] 确认 launchd 环境 secret 可解析（OPENCLAW_GATEWAY_TOKEN 等）
- [ ] `tailscale serve status` 预检 stale 443 路由
- [ ] 更新 → `plugins enable hcc-memory/codex --accept-capabilities` → 起网关
- [ ] 自证五连：memory_search 命中 / harvester 心跳 / 注入干净 / 微信收发 / dreaming cron 不双开
- [ ] 回滚绳：`npm -g i openclaw@2026.7.1-2` + 备份还原（jsonl 归档还在即无损）

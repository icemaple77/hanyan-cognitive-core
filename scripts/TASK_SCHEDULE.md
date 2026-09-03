# Task-Schedule — agent 长任务防停摆(看板卡 t_6b29b140)

把 Claude Code「循环续跑 + 活待办 + 完成回调」外部化到 hermes / openclaw 身上:
长任务拆成有序步骤存进 HCC,一条常驻 cron 按到期时间唤醒**零记忆新会话**,
新会话跑 `verify_cmd` 从真实世界读进度(不凭记忆猜)、推进一步、回报,直到完成。

> **驱动收口(2026-09-03)**:整套=**一个**任务管理系统——
> - **后端/接口(共享真相源)**:状态机 + `/api/v1/tasks/*` REST + MCP `task_*` 工具。
>   openclaw 等第三方 agent 通过它注册/推进长任务,防中断靠这层。
> - **驱动(唯一)**:HanyanOS core 的 **`work-driver`**(Go,受 core 监督,加固:spawn 硬
>   超时 + 并发闸防惊群),`HCC_AGENT_ID`/`HCC_SPAWN_CMD` 可配,一个实例驱动一个 runtime。
> - 本文早先的 `scripts/task_driver.sh` + `.plist` 是 **bash 版驱动,已退役,勿加载**——
>   与 work-driver **二选一**,同时跑会对同一到期任务双重派活(惊群)。给 openclaw 加防中断
>   = 起一个 `HCC_AGENT_ID=openclaw` + openclaw 会话 spawn 命令的 work-driver 实例。

## 组成

| 层 | 位置 |
|---|---|
| 表 | `gateway/models/__init__.py` — `Task` / `TaskStep` |
| 状态机 | `gateway/services/task_service.py` — register/wake/report/cancel + 租约、attempt 上限、红线、时间校准、backoff |
| MCP 工具 | `mcp/task_tools.py` + `mcp/server.py` — `task_create/get/due/wake/report/cancel`(会话内 agent 直接调) |
| REST | `gateway/api/task_routes.py` — `/tasks`、`/tasks/due`、`/wake`、`/report`、`/cancel`(给 cron 驱动) |
| **cron 驱动** | `scripts/task_driver.sh` — poll due → wake → spawn 无人值守会话 / escalate |
| **调度** | `scripts/com.hanyan.hcc-task-driver.plist` — launchd,每 120s 跑一次驱动 |

## 闭环

```
cron(120s) → GET /tasks/due?agent_id=hanyan
           → 每个到期任务 POST /tasks/{id}/wake
               action=work     → spawn `claude -p` 无人值守会话,注入 prompt
                                  会话:跑 verify_cmd → 干活 → task_report
               action=escalate → 通知主人(HCC_NOTIFY_CMD / 日志),不自动干
```

**租约**:wake 会把 `next_wake_at` 推到 `now + est`,任务被唤醒后在会话汇报前不会被重复列为 due——避免每个 tick 重复 spawn。会话死了没汇报,租约到期自动再唤醒(安全网),attempt 计到上限则 BLOCKED 升级。

**确定性**:进度只认新会话 `verify_cmd` 的结果,不认 agent 自述。

## 启用(mac / hanyan)

```bash
cp scripts/com.hanyan.hcc-task-driver.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.hanyan.hcc-task-driver.plist
```

停:`launchctl unload ~/Library/LaunchAgents/com.hanyan.hcc-task-driver.plist`
日志:`~/.hcc/task-driver/{driver,spawn-*,escalations}.log`

先干跑一轮不 spawn:`DRIVER_DRYRUN=1 HCC_AGENT_ID=hanyan bash scripts/task_driver.sh`

## 无人值守会话的权限

默认 spawn 用 `claude -p --dangerously-skip-permissions`——无人值守要能跑 bash / 改文件而没人批准。
危险步骤(删除/花钱/对外/家庭域)被服务端红线拦在 `work` 之前(改走 escalate),不会进无人值守。
要收紧:设 `HCC_SPAWN_CMD`(如加 `--allowedTools`),或接 hermes/openclaw 自己的开会话机制。
要把升级路由到飞书/微信:设 `HCC_NOTIFY_CMD`(prompt 从 stdin 进)。

## openclaw / n100

n100 上的 openclaw 用它自己的定时机制,照 `scripts/task_driver.sh` 的三步(poll due → wake → spawn/escalate)
在它的运行时里实现等价驱动即可;REST 面(`/api/v1/tasks/*`)跨运行时通用。

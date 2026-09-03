# _archive — 退役文件(勿在运行时使用)

这里放已被取代、但保留作参考的东西。**不要从这里加载/运行任何文件。**

## task_driver.sh + com.hanyan.hcc-task-driver.plist(2026-09-03 退役)
Task-Schedule 的早期 **bash 版驱动**。已被 **HanyanOS core 的 `work-driver`**(Go,受 core
监督,加固:spawn 硬超时 + 并发闸防惊群)取代。二者**二选一**,同时跑会对同一到期任务
双重派活(惊群)。任务后端/接口(`/api/v1/tasks/*` + MCP `task_*`)仍是共享真相源,不受影响。
详见 `scripts/TASK_SCHEDULE.md`「驱动收口」。

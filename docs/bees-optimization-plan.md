# 上海 NAS volume3 BEES 去重优化方案

- **状态**: 设计方案，尚未实施
- **调研方式**: `ssh nas-sh` 只读命令（未对生产做任何修改）
- **调研日期**: 2026-08-08
- **目标卷**: `/volume3`（DS920+，DSM 7.2.2-72806，kernel 4.4.302+，btrfs 定制内核），5.3T，已用 3.8T（71%）
- **BEES**: `/usr/local/bin/bees` v0.11-50-g2d53565，静态编译

---

## TL;DR

| 项 | 结论 |
|----|------|
| 8/5 故障根因 | **DSM 自带的 `synologrotated` 对 `/var/log/bees-volume3.log` 做了 "emergency logrotate"**（因为没有对应的 `/etc/logrotate.d` 配置），时间点与 BEES 进程死亡精确重合。锁目录残留是**次生故障**：原脚本在 `exec` 替换进程映像后，`trap ... EXIT` 已经失效，BEES 意外退出时没有任何东西负责清理锁 |
| hash 表回落到 128KB 根因 | 无法 100% 取证到具体触发事件（日志留存不够早），但可确认：本次故障与 hash 表大小无关（表在崩溃前就已经是 128KB），且现有脚本对 hash 表大小没有任何保障机制——**这是设计缺陷，不是一次性意外**，必须做成幂等自愈，不再依赖"手动设置一次就永久有效"的假设 |
| 看门狗 | 当前两条 crontab 都用了 `pgrep -f`——**DSM ash 下 `pgrep` 命令不存在**（实测 `exit 127: command not found`），只是因为 `\|\|` 逻辑，pgrep 报错也会触发调用 `start-bees.sh`，看门狗本质上"意外能用"但完全是走运，且每 5 分钟制造一次垃圾日志/失败调用 |
| 最小必要改动 | 1) 新 `start-bees.sh`（PID 自愈锁 + hash 表校验）2) `/etc/logrotate.d/bees-volume3`（根治崩溃触发源）3) crontab 合并为一行、去掉 `pgrep` |
| 可选改动 | 监控脚本 `bees-status.sh` + Hermes 巡检、thread-count 调优、systemd unit 替代 crontab |
| 3.8T 重扫预估 | 单线程 loadavg-target 2.0 下，约 **20–30 小时**（见"时间预估"一节的推导） |

---

## 1. 现场调研结果（只读，2026-08-08）

```
df -h /volume3
  /dev/mapper/cachedev_0  5.3T  3.8T  1.6T  71%  /volume3

mount | grep volume3
  /dev/mapper/cachedev_0 on /volume3 subvolid=256,subvol=/@syno  (DSM 视图)
  /dev/mapper/cachedev_2 on /volume3/bees_root subvolid=5,subvol=/  (BEES 用的原始根，当前挂载正常)

/var/run/bees-volume3.lock/       # 目录仍然存在，Aug 5 13:17 创建，内部无任何文件（无 PID 记录）
ps | grep bees                    # 空——BEES 进程已不存在
.beeshome/beeshash.dat            # 131072 bytes = 128KB（默认值，不是 7/25 设的 1GB）
.beeshome/beescrawl.dat           # 存在，记录着 Aug 5 17:13-17:17 的 crawl 断点（root 5 等多个 subvol 的 transid）
.beeshome/beesstats.txt           # Now: 2026-08-05-17-17-08, Uptime: 14400.4s（本次运行了 4 小时后停止）
                                   # hash 表占用率 "8192/8192 cells occupied, 100%" —— 表早就满了
/var/log/bees-volume3.log         # 当前 0 字节（Aug 5 17:17 之后没有新写入）
/var/log/bees-volume3.log.1.xz    # Aug 5 17:12 —— 最近一次自动轮转
/var/log/bees-volume3.log.2.xz    # Jul 17 23:20
/var/log/bees-volume3.log.3.xz    # Jul 15 21:52

pgrep -f bees                     # ash: pgrep: command not found  (exit 127) —— 确认 DSM ash 无 pgrep
uptime                            # up 3 days 7:04（从当前时间反推，系统在 Aug 5 ~13:16 重启过，与 BEES 13:17 启动时间吻合）
dmesg | grep -iE "oom|kill|panic|segfault"   # 无匹配（32MB 环形缓冲区覆盖了整个故障窗口）——排除 OOM/内核态杀进程/段错误
free -h                           # 15G 内存，1.2G 已用，10G swap 几乎空闲 —— 也排除内存压力
```

**关键线索**（`/var/log/messages`）：

```
2026-08-05T17:17:30+08:00 NAS-SH synologrotated[8929]: synologrotated.cpp:858
    Can't find logrotate conf of /var/log/bees-volume3.log, do emergency logrotate
```

这条消息的时间戳（17:17:30）与 `.beeshome/*` 三个文件的最后写入时间（17:17:08）以及 BEES 日志停止更新的时间几乎完全重合，全库检索也只有这一条记录——是本次故障窗口内**唯一**与 bees 相关的异常事件。`/etc/logrotate.d/` 下确认没有任何 bees 相关配置文件（列出了全部 60 个文件，全是 DSM 包自带的）。

---

## 2. 逐项根因分析 + 优化设计

### 2.1 锁机制

**根因（两层）**：

1. **次生表现**：锁目录 `mkdir "$LOCKDIR"` 只做"存在性"判断，不判断锁的主人是否还活着。BEES 死后锁目录残留，看门狗永远认为"already running"。
2. **真正的设计缺陷**：原脚本用 `trap 'rmdir "$LOCKDIR"' EXIT INT TERM` 期望进程退出时自动清理，但脚本随后 `exec "$BEES" ...` —— **`exec` 会用 BEES 的进程镜像替换当前 shell 镜像，shell 进程连同它注册的 trap 一起消失**。之后无论 BEES 是正常退出、被信号杀死还是崩溃，都不会有任何机制去 `rmdir`。这个 trap 只在 "exec 之前" 脚本自己异常退出时才有效，对"BEES 启动后死亡"这一最常见场景完全无效。这就是为什么锁会永久残留。

**设计（治本）**：锁目录里存 PID 文件，启动前读 PID、查 `/proc/$PID` 是否存活、且 cmdline 确实是 bees，判定为"死锁"则自动清除后重新加锁：

```sh
LOCKDIR="/var/run/bees-volume3.lock"
PIDFILE="$LOCKDIR/bees.pid"

if [ -d "$LOCKDIR" ]; then
    OLDPID=""
    [ -f "$PIDFILE" ] && OLDPID=$(cat "$PIDFILE" 2>/dev/null)
    if [ -n "$OLDPID" ] && [ -d "/proc/$OLDPID" ] \
       && tr '\0' ' ' < "/proc/$OLDPID/cmdline" 2>/dev/null | grep -q bees; then
        echo "$(date) BEES already running (pid=$OLDPID) on $ROOT" >> "$LOG"
        exit 0
    fi
    echo "$(date) Stale lock detected (pid=${OLDPID:-unknown} not alive) - cleaning up" >> "$LOG"
    rm -rf "$LOCKDIR"
fi

if ! mkdir "$LOCKDIR" 2>/dev/null; then
    # 两个看门狗几乎同时触发，输了 mkdir 竞争的一方直接退出即可，不是错误
    echo "$(date) Lost startup race with a concurrent invocation, exiting" >> "$LOG"
    exit 0
fi
echo $$ > "$PIDFILE"
```

- 用 `/proc/$PID/cmdline`（procfs 直接查）而不是 `ps`，避免依赖 busybox `ps` 的输出格式差异，也避免 `ps | grep bees` 常见的自匹配问题（下面 2.3 节会用实测证据说明这个坑）。
- `echo $$ > "$PIDFILE"` 必须放在 `exec "$BEES"` **之前**：因为 `exec` 不新建进程、只替换镜像，此时 `$$` 记录的 shell PID 之后就等于 BEES 的 PID，天然正确，不需要额外拿 BEES 自己的 PID。
- `mkdir` 仍然是清锁后重新获取锁的原子点，两个并发看门狗调用最多一个能成功创建目录，不会双启动。
- 保留（但不依赖）一个尽力而为的 `trap` 覆盖 exec 之前的异常路径（比如挂载失败提前退出），单纯是卫生习惯，不是安全网。

**已知残余风险**（写进文档，不隐藏）：清锁 (`rm -rf`) 到重新加锁 (`mkdir`) 之间有极小的 TOCTOU 窗口，如果恰好另一个真正合法的新实例在这几毫秒内完成了「创建目录但还没写 PID 文件」，会被误判为死锁而清除。给定看门狗 5 分钟一次、脚本这段执行只有几毫秒，概率可忽略，无需额外处理。

### 2.2 hash 表自动保障

**根因**：无法百分之百取证到 7/25 → 8/5 之间具体是哪次操作把 hash 表打回了 128KB（`/var/log/messages` 只保留到 7/25 16:11 之后，早于疑似丢失窗口的更早部分已经轮转不可查）。但已确认两件事：

1. 本次 8/5 崩溃**不是**由 hash 表引起（崩溃前它就已经是 128KB，崩溃本身是 logrotate 触发的，见 2.6）。
2. 更重要的是：**原脚本从未对 hash 表大小做任何校验**——不管当初是怎么丢的，只要机制上允许"没人管、默认值又会被谁不小心带回来"，这个问题迟早复发。所以正确的设计目标不是去当侦探，而是让脚本自己在每次启动前**幂等地**保证 hash 表 ≥ 1GB，把"手动设置一次、祈祷它一直有效"变成"每次启动都自检自愈"。

**设计**：

```sh
HASHFILE="$ROOT/.beeshome/beeshash.dat"
HASH_TARGET_BYTES=1073741824   # 1GiB，来自 hash-table-sizing.md：5.3T 卷的安全值，覆盖到 ~8T

mkdir -p "$ROOT/.beeshome"
CURSIZE=0
[ -f "$HASHFILE" ] && CURSIZE=$(stat -c %s "$HASHFILE" 2>/dev/null || echo 0)

if [ "$CURSIZE" -lt "$HASH_TARGET_BYTES" ]; then
    echo "$(date) hash table undersized (current=${CURSIZE} bytes, target=${HASH_TARGET_BYTES}) - rebuilding to 1G" >> "$LOG"
    rm -f "$HASHFILE"
    truncate -s 1G "$HASHFILE"
    chmod 700 "$HASHFILE"
    # hash 缓存被清空后，旧数据不会自动被重新比对，除非强制让 crawl 从头开始
    rm -f "$ROOT/.beeshome/beescrawl.dat"
    echo "$(date) hash table rebuilt, crawl state reset for full rescan" >> "$LOG"
fi
```

- `stat -c %s`：DSM ash busybox 自带 `stat`，已在调研中确认可用（`stat /volume3/bees_root/.beeshome` 正常返回）。
- 1GB 是 128KB 的整数倍（1073741824 / 131072 = 8192，正好对应当前 `beesstats.txt` 里 "8192/8192 cells" 的表结构），满足 BEES 对 hash 表大小的对齐要求。
- **只有在真正需要重建时才删 `beescrawl.dat`**——这是与 2.4 节"崩溃自愈"呼应的关键设计：正常的崩溃重启 **不会**触碰 crawl 断点，只有 hash 表被重建（表示旧数据全部需要重新比对）时才强制全卷重扫，逻辑上是自洽的。
- 这段逻辑幂等：一旦表被建到 1GB，以后每次重启（哪怕是看门狗每 5 分钟调一次）都只是 `CURSIZE < TARGET` 判 false，直接跳过，不会重复重建、不会重复清空 crawl 状态。

### 2.3 看门狗去重 + 防误匹配

**现状**（`/etc/crontab` 原文）：

```
*/5 * * * * root pgrep -f "/usr/local/bin/bees.*volume3" >/dev/null || /volume3/scripts/start-bees.sh
@reboot root /volume3/scripts/start-bees.sh
*/5 * * * * root pgrep -f /usr/local/bin/bees >/dev/null || /volume3/scripts/start-bees.sh
```

**根因**：

1. **`pgrep` 在 DSM ash 下不存在**——实测 `pgrep -f bees` 返回 `ash: pgrep: command not found`，exit 127。这两条看门狗规则实际上从部署第一天起就没有"真正检测到进程存活"过；只是因为 `command_not_found || fallback` 里 127 也是非零退出码，`||` 恒真触发 `start-bees.sh`，而 `start-bees.sh` 内部的锁目录才是唯一真正生效的防重复机制。换句话说，这两条 crontab 现在的实际行为等价于"每 5 分钟无条件调用一次 start-bees.sh"，只是每次都先在 stderr 里报一次 "command not found"（虽然 `MAILTO=""` 使其不会寄邮件，但仍是噪音、且完全依赖脚本内部锁兜底，一旦锁逻辑本身失效——就是本次故障——看门狗对此毫无感知）。
2. 两条规则功能重复，只是过滤条件不同（一条带 `.*volume3` 一条不带），维护上容易改一条漏一条。

**"看门狗会不会误匹配"的验证**：会。调研过程中一条普通的 `ps ... | grep -i bees` 命令，其**自身进程**的命令行里就包含字符串 "bees"（因为 grep 的参数就是 "bees"），被自己匹配上——这是 `pgrep -f` / `ps | grep` 模式的经典陷阱（需要额外 `grep -v grep` 或 `grep "[b]ees"` 技巧规避）。既然新方案已经不再依赖任何全局字符串搜索进程表（改用 2.1 节里"读记录的 PID → 查 `/proc/$PID/cmdline`"的精确匹配），这个风险已经在设计层面消除，不需要额外过滤技巧。

**设计**：既然 `start-bees.sh` 内部（2.1 节）已经能自己判断"该不该真的启动 BEES"，看门狗根本不需要在 crontab 层面重复做存活检测——直接无条件调用脚本，把判断逻辑留给脚本自己：

```
# /etc/crontab（仅展示 bees 相关行，其余 DSM 自带任务不变）
@reboot root /volume3/scripts/start-bees.sh
*/5 * * * * root /volume3/scripts/start-bees.sh >/dev/null 2>&1
```

- 合并为一条 5 分钟规则，去掉 `pgrep`。
- `@reboot` 保留——本次调研意外确认了它在这台机器上确实生效（BEES 在系统重启后的 13:17 准时启动，与系统 uptime 反推的重启时间吻合），推翻了通用参考文档里"DSM 不支持 crontab `@reboot`，需要用任务计划 GUI"的保守说法（那条建议来自旧版本 DSM 的经验，这台 7.2.2-72806 是有 systemd 的新版本，`@reboot` 实测可用）。仍建议保留任务计划 GUI 作为 fallback 的说明，但不是必须。
- `>/dev/null 2>&1`：新脚本所有真实状态都写到 `$LOG`，crontab 本身不需要再产生输出。

### 2.4 崩溃自愈 / `beescrawl.dat` 的作用

`beescrawl.dat` 记录了每个 btrfs 子卷（root）当前爬到的 objectid / offset / transid（`min_transid` / `max_transid`）。连续模式（`--scan-mode 0`）启动时会读取这个文件，从记录的位置继续爬，而不是从零开始遍历整卷——这是 BEES 崩溃重启后能"续跑"而不用每次全量重扫的核心机制。

调研中实际看到的内容（截取）：

```
root 5   objectid 276      offset 18446744073709486080  min_transid 935034 max_transid 935035  start_ts 2026-08-05-17-13-18
root 259 objectid 4961628  offset 18446744073709486080  min_transid 934539 max_transid 935035  start_ts 2026-08-05-17-16-50
...
```

**设计原则**：

- 新 `start-bees.sh` **默认绝不删除 `beescrawl.dat`**——普通的崩溃/看门狗重启，续跑逻辑完全交给 BEES 自己的机制处理，脚本不干预。
- 唯一的例外是 2.2 节里 hash 表被重建的分支：hash 缓存清空后，旧数据即使 crawl 断点跳过了也不会被重新比对，所以那种情况必须连带清掉 `beescrawl.dat` 强制全卷重扫——这是本次上线**唯一**一次会触发全卷重扫的原因（因为 hash 表当前是 128KB，必然会被重建到 1GB），之后的任何崩溃重启都不会再触发。

### 2.5 监控与报告

设计一个只读状态脚本 `/volume3/scripts/bees-status.sh`，可以安全地随时执行、随时被 Hermes 定时调用：

```sh
#!/bin/sh
ROOT="/volume3/bees_root"
LOG="/var/log/bees-volume3.log"
LOCKDIR="/var/run/bees-volume3.lock"
PIDFILE="$LOCKDIR/bees.pid"

echo "== volume3 space =="
df -h /volume3

echo "== bees process =="
if [ -f "$PIDFILE" ]; then
    PID=$(cat "$PIDFILE" 2>/dev/null)
    if [ -n "$PID" ] && [ -d "/proc/$PID" ] \
       && tr '\0' ' ' < "/proc/$PID/cmdline" 2>/dev/null | grep -q bees; then
        echo "RUNNING pid=$PID"
        ps -o pid,etime,rss,cmd -p "$PID" 2>/dev/null
    else
        echo "DEAD (lock present, pid=${PID:-unknown} not alive) -- watchdog should self-heal within 5min"
    fi
else
    echo "NOT RUNNING (no lock)"
fi

echo "== hash table =="
if [ -f "$ROOT/.beeshome/beeshash.dat" ]; then
    SIZE=$(stat -c %s "$ROOT/.beeshome/beeshash.dat")
    echo "size=${SIZE} bytes ($((SIZE/1024/1024)) MB)"
else
    echo "MISSING"
fi

echo "== dedup stats (this session) =="
grep -E "^Now:|^Uptime:|dedup_bytes=|dedup_hit=" "$ROOT/.beeshome/beesstats.txt" 2>/dev/null

echo "== recent log tail =="
tail -15 "$LOG" 2>/dev/null
```

**用途和限制**：

- 全部是只读操作（`df`、`ps`、`stat`、`cat`、`tail`），随时可跑，不影响生产。
- `beesstats.txt` 里的 `dedup_bytes` / `dedup_hit` 是**本次 BEES 进程运行期间的累计值**（对照 `Uptime` 字段），进程重启后清零，不是"历史总节省空间"。要看真正的空间节省趋势，需要定期记录 `df -h /volume3` 或 `btrfs filesystem usage /volume3` 的 Used 值做前后对比，脚本里已经包含 `df -h`，如果需要更精确的已分配/已用区分可以追加 `btrfs filesystem usage /volume3`（本次调研为保持"只读、最小化"未额外验证该命令输出格式，实施时可以顺手加上）。
- 建议 Hermes 侧调用方式：cronjob 每 30-60 分钟通过 `ssh nas-sh bash /volume3/scripts/bees-status.sh` 拉取输出，写入 kanban 或简报；不需要在 NAS 上额外部署任何东西，脚本本身就是完整的独立单元。

### 2.6 新数据去重策略

3.8T 的 USB 迁移数据已经落盘到 volume3。**不需要**额外手动跑 `--scan-mode 2`（增量模式）：

- 当前脚本已经用 `--scan-mode 0`（连续模式），BEES 会持续轮询 btrfs 的 transid，任何新写入的数据（不管是 rsync、USB 迁移还是其它方式写入的）都会在下一轮轮询（约 30-55 秒一次）里自动被发现并加入 crawl 队列，不需要人工触发。
- 唯一真正需要的动作是让 BEES **稳定地一直活着**——这正是 2.1-2.3 节修复的内容。一旦上线，2.2 节的 hash 表重建会顺带触发一次全卷重扫（因为 crawl 断点被清空），这次重扫本身就会覆盖全部新旧数据，包括刚迁移进来的 3.8T，不需要为"新数据"单独设计一套流程。
- 不建议手动并行跑一次性 `--scan-mode 1/2`：BEES 不支持同一个 root 被多个实例同时处理，2.1 节的锁已经会阻止手动启动的第二实例，多此一举。

### 2.7 日志与 logrotate 兼容性

**这不只是"确认兼容性"的问题——这就是本次故障的根本触发源。**

现有的 `.xz` 轮转历史（`.1.xz` Aug 5 17:12、`.2.xz` Jul 17 23:20、`.3.xz` Jul 15 21:52）容易让人误以为是标准 `logrotate`（`/etc/logrotate.conf` 里配的全局 `rotate 4 / size 1M / compress xz`）在生效。但实际检查 `/etc/logrotate.d/` 全部 60 个文件，**没有任何一个是给 `bees-volume3.log` 用的**。真正做这件事的是 DSM 自带的 `synologrotated` 守护进程，它的行为在日志里说得很清楚：

```
synologrotated.cpp:858 Can't find logrotate conf of /var/log/bees-volume3.log, do emergency logrotate
```

即：当它发现某个 `/var/log/*.log` 文件超过阈值、但又找不到对应的 `logrotate.d` 配置时，会走一条"emergency"应急分支去处理这个文件，绕过标准 logrotate 的 `copytruncate`/信号通知等安全机制。这条消息的时间戳（17:17:30）与 BEES 最后一次写入 `.beeshome`（17:17:08）、日志停止更新的时间点几乎重合到秒级，且是整个故障窗口里唯一的相关异常记录——这是目前能拿到的最强证据链，指向"emergency logrotate 处理 BEES 正在写入的日志文件时，以某种方式导致了 BEES 进程终止"。

**设计（根治）**：给这个日志文件补一个正常的 `logrotate.d` 配置，让 `synologrotated` 的判断条件（"找不到 conf"）从此不再成立，emergency 分支永远不会被触发：

```
# /etc/logrotate.d/bees-volume3
/var/log/bees-volume3.log {
    rotate 4
    size 1M
    compress
    compresscmd /usr/bin/xz
    compressext .xz
    compressoptions -3
    missingok
    notifempty
    copytruncate
}
```

- 参数照搬 `/etc/logrotate.conf` 里的全局默认值（`rotate 4`、`size 1M`、`xz -3`），保持与系统其它日志一致的留存策略，唯一新增的是显式的路径匹配和 `copytruncate`。
- `copytruncate`：轮转时"复制内容出去再原地截断"而不是"改名+建新文件"，保证 BEES 进程持有的日志文件描述符（`exec ... >> "$LOG" 2>&1` 继承来的 fd）永远指向同一个 inode，彻底避免任何关于 fd 失效的疑虑，不依赖 BEES 自己去 handle 重新打开日志文件（它也确实没有这个逻辑）。
- **持久性提醒**：`/etc/logrotate.d/` 和 `/etc/crontab` 一样是系统区，DSM 版本升级可能会重置/清空这类自定义文件。建议把这份配置的副本存放在 `/volume3/scripts/bees-volume3.logrotate`（版本化留档），DSM 更新后可以快速比对/恢复。

---

## 3. 完整新脚本

### 3.1 `/volume3/scripts/start-bees.sh`（全文）

```sh
#!/bin/sh
# BEES startup script — volume3 (self-healing lock + hash-table guard)
#
# --scan-mode 0 = continuous daemon (crawl once, then watch for new transactions)
#
# DSM ash 兼容性注意：无 pgrep/pkill/mountpoint，全部用 ps/proc/grep -qs 代替

BEES="/usr/local/bin/bees"
DEV="/dev/mapper/cachedev_2"
ROOT="/volume3/bees_root"
LOG="/var/log/bees-volume3.log"
LOCKDIR="/var/run/bees-volume3.lock"
PIDFILE="$LOCKDIR/bees.pid"
HASHFILE="$ROOT/.beeshome/beeshash.dat"
HASH_TARGET_BYTES=1073741824   # 1GiB — 5.3T 卷的安全值（覆盖到 ~8T），见 hash-table-sizing.md

# 等待卷挂载就绪（开机时可能还没就绪）
for i in $(seq 1 60); do
    [ -d "/volume3" ] && break
    sleep 1
done

# 挂载 btrfs 根子卷（subvolid=5）—— BEES 要求，不能用 DSM 的 subvolid=256 视图
mkdir -p "$ROOT"
if ! grep -qs "$ROOT" /proc/mounts; then
    mount -o subvolid=5 "$DEV" "$ROOT" 2>/dev/null
fi
if ! grep -qs "$ROOT" /proc/mounts; then
    echo "$(date) FATAL: failed to mount $ROOT (subvolid=5) — aborting start" >> "$LOG"
    exit 1
fi

# .beeshome 是 BEES 存放 hash 表 / crawl 状态的强制目录
mkdir -p "$ROOT/.beeshome"

# --- 1) PID 自愈锁：死锁自动清理，不再依赖 exec 之后已失效的 EXIT trap ---
if [ -d "$LOCKDIR" ]; then
    OLDPID=""
    [ -f "$PIDFILE" ] && OLDPID=$(cat "$PIDFILE" 2>/dev/null)
    if [ -n "$OLDPID" ] && [ -d "/proc/$OLDPID" ] \
       && tr '\0' ' ' < "/proc/$OLDPID/cmdline" 2>/dev/null | grep -q bees; then
        echo "$(date) BEES already running (pid=$OLDPID) on $ROOT" >> "$LOG"
        exit 0
    fi
    echo "$(date) Stale lock detected (pid=${OLDPID:-unknown} not alive) - cleaning up" >> "$LOG"
    rm -rf "$LOCKDIR"
fi

if ! mkdir "$LOCKDIR" 2>/dev/null; then
    echo "$(date) Lost startup race with a concurrent invocation, exiting" >> "$LOG"
    exit 0
fi
echo $$ > "$PIDFILE"
# 尽力而为的清理：只在 exec 之前的异常退出路径生效（比如上面挂载失败的 exit 1），
# BEES 启动之后的任何退出都不会经过这个 trap —— 真正的安全网是上面的 PID 存活检测。
trap 'rm -rf "$LOCKDIR"' EXIT INT TERM

# --- 2) hash 表自愈：不足 1GB 自动重建（幂等，达标后不再触发） ---
CURSIZE=0
[ -f "$HASHFILE" ] && CURSIZE=$(stat -c %s "$HASHFILE" 2>/dev/null || echo 0)
if [ "$CURSIZE" -lt "$HASH_TARGET_BYTES" ]; then
    echo "$(date) hash table undersized (current=${CURSIZE} bytes, target=${HASH_TARGET_BYTES}) - rebuilding to 1G" >> "$LOG"
    rm -f "$HASHFILE"
    truncate -s 1G "$HASHFILE"
    chmod 700 "$HASHFILE"
    # hash 缓存清空后旧数据不会被自动重新比对，必须清掉 crawl 断点强制全卷重扫
    rm -f "$ROOT/.beeshome/beescrawl.dat"
    echo "$(date) hash table rebuilt, crawl state reset for full rescan" >> "$LOG"
fi

echo "========== $(date) ==========" >> "$LOG"
echo "Starting BEES on $ROOT (pid will be $$)" >> "$LOG"

exec "$BEES" \
    --thread-count 1 \
    --loadavg-target 2.0 \
    --scan-mode 0 \
    "$ROOT" >> "$LOG" 2>&1
```

**与原脚本的差异总览**：

| 项 | 原脚本 | 新脚本 |
|----|--------|--------|
| 锁 | `mkdir` 存在性判断 + `trap EXIT`（exec 后失效） | PID 存活判断（`/proc/$PID/cmdline`），死锁自动清 |
| hash 表 | 无任何检查 | 启动前校验大小，不足 1GB 自动 `truncate` 重建 |
| crawl 状态 | 未涉及 | 仅在 hash 重建分支清空，其余场景保留（自愈续跑） |
| 挂载失败处理 | 静默继续（`2>/dev/null` 吞掉错误后无检查） | 挂载后二次校验，失败则 `FATAL` 记日志并 `exit 1` |
| `loadavg-target` | 2.0（7/14 定的） | 保持 2.0 不变 |

### 3.2 `/etc/logrotate.d/bees-volume3`（新增文件）

见 2.7 节，全文已给出。同时建议在 `/volume3/scripts/bees-volume3.logrotate` 留一份副本存档。

### 3.3 crontab 改动（`/etc/crontab`）

**删除**这三行：
```
*/5 * * * * root pgrep -f "/usr/local/bin/bees.*volume3" >/dev/null || /volume3/scripts/start-bees.sh
@reboot root /volume3/scripts/start-bees.sh
*/5 * * * * root pgrep -f /usr/local/bin/bees >/dev/null || /volume3/scripts/start-bees.sh
```

**替换为**：
```
@reboot root /volume3/scripts/start-bees.sh
*/5 * * * * root /volume3/scripts/start-bees.sh >/dev/null 2>&1
```

其余 4 条 `synoschedtask` DSM 自带任务不受影响，原样保留。

### 3.4 监控脚本 `/volume3/scripts/bees-status.sh`（新增，可选）

见 2.5 节全文。

---

## 4. 验证计划

全部验证优先在**非生产路径**（`/tmp` 沙箱或专门构造的假状态）里做，只有最后一步才在真实 `LOCKDIR`/`HASHFILE` 上验证，且每一步都可以立刻回滚。

### 4.1 验证 PID 自愈锁

1. 沙箱验证锁逻辑本身（不碰生产路径）：
   ```sh
   TESTDIR=/tmp/bees-lock-test
   rm -rf "$TESTDIR"; mkdir -p "$TESTDIR"
   mkdir "$TESTDIR/lock"
   echo 999999 > "$TESTDIR/lock/bees.pid"   # 999999 几乎不可能是活着的 PID
   # 用等价的 shell 判断逻辑跑一遍，确认能识别出 999999 不在 /proc 下 → 判定死锁 → 清理
   ```
2. 真实场景验证（当前 `/var/run/bees-volume3.lock` 正好就是一个残留死锁，天然的活案例）：部署新脚本后手动执行一次 `/volume3/scripts/start-bees.sh`，预期日志出现：
   ```
   Stale lock detected (pid=unknown not alive) - cleaning up
   ...
   Starting BEES on /volume3/bees_root (pid will be <新PID>)
   ```
   随后 `cat /var/run/bees-volume3.lock/bees.pid` 应该等于新启动的 BEES 进程 PID，`/proc/<PID>/cmdline` 应该含 "bees"。
3. 双看门狗竞争验证：手动同时（后台 `&`）触发两次 `start-bees.sh`，预期只有一个真正 `exec` 进 BEES，另一个日志里出现 "Lost startup race" 或 "already running"，`ps` 里只有一个 bees 进程。

### 4.2 验证 hash 表自愈

1. 先在**当前 128KB 的真实文件**上验证触发条件（这本来就是本次要修的问题，不需要额外造假数据）：部署新脚本、执行一次，预期日志出现：
   ```
   hash table undersized (current=131072 bytes, target=1073741824) - rebuilding to 1G
   hash table rebuilt, crawl state reset for full rescan
   ```
   随后 `stat -c %s .beeshome/beeshash.dat` 应为 1073741824，`ls .beeshome/beescrawl.dat` 应不存在（等 BEES 跑起来后会自动重新创建）。
2. 幂等性验证：hash 表已经是 1GB 之后，再次手动执行 `start-bees.sh`（此时 BEES 正在跑，会走"already running"分支提前退出，不会碰到 hash 表逻辑——如需单独验证幂等性，可以先 `kill` 掉 BEES 再立刻重启一次），确认第二次不再出现 "rebuilding" 日志、`beescrawl.dat` 不会被误删。

### 4.3 验证看门狗

1. 部署 crontab 改动后，故意 `kill` 一次 BEES 进程（模拟崩溃），等待 ≤5 分钟，用 `bees-status.sh` 或 `ps | grep bees` 确认看门狗自动把它拉起来了，且新 PID 已写入 `bees.pid`。
2. 检查 `/var/log/messages` 或 cron 自身日志，确认不再出现 `pgrep: command not found` 报错。

### 4.4 验证 logrotate 修复

1. 部署 `/etc/logrotate.d/bees-volume3` 后，找一种安全方式提前触发一次 rotate 来验证（而不是等日志自然长到 1MB）：`logrotate -f /etc/logrotate.d/bees-volume3`（如果 DSM 提供标准 `logrotate` 命令；如果没有，退而求其次是持续观察下一次自然触发时 BEES 是否还活着）。
2. 核心验证目标：rotate 完成后，`ps | grep bees` 里的 BEES 进程 **PID 不变**（因为是 `copytruncate`，进程没有被打断），`tail -f "$LOG"` 能看到 rotate 之后继续有新内容写入（而不是像 8/5 那样彻底停更）。
3. 长期验证：这条修复的真正效果要等到日志自然长到 1MB 触发下一次真实 rotate（预计几天到几周后，取决于日志增长速度）才能最终确认，建议上线后用 4.4 节的监控脚本持续观察，看是否还会复现 "找不到 conf" 的 messages 日志。

### 4.5 回归验证（SHA256）

沿用 7/14 已验证过的方法，在 volume3 上任选一批文件做一次 dedup 前后 SHA256 对比，确认新脚本没有引入任何数据完整性风险（新脚本对 BEES 的调用参数完全没变，理论上风险为零，这一步是保险起见）。

---

## 5. 回滚方案

| 改动 | 回滚方式 |
|------|----------|
| `start-bees.sh` | 上线前用 `cp start-bees.sh start-bees.sh.bak-20260808` 存档旧版本；回滚 = 用备份覆盖回去，`kill` 当前 BEES 进程，等看门狗或手动重启用旧脚本拉起 |
| `/etc/logrotate.d/bees-volume3` | 全新文件，回滚 = 直接 `rm` 掉即可，恢复到"没有配置"的原状态（虽然那正是本次故障的诱因，仅在新配置本身出问题时才需要这么做） |
| crontab | 上线前 `cp /etc/crontab /etc/crontab.bak-20260808`；回滚 = 用备份覆盖 |
| `bees-status.sh` | 全新只读脚本，直接删除文件即可，无副作用 |
| hash 表重建 | **不可逆的一次性动作**（旧 128KB 数据本来就没有价值，重建是本方案的目的之一，不存在"回滚 hash 表"的需求）；如果重建后发现空间不够或效果不达预期，可以后续再手动调整目标大小重跑一次 truncate，属于正常运维而非回滚 |

所有回滚操作都不涉及删除用户数据，风险仅限于"BEES 服务本身暂停"，不影响 volume3 上文件的可用性。

---

## 6. 时间预估：3.8T 重扫要多久

用当前 `beesstats.txt` 里最后一次运行（8/5 13:17-17:17，共 14400.4 秒 ≈ 4 小时）的真实数据反推吞吐：

- `block_bytes` = 748,484,041,734 字节 ≈ 697 GiB（这 4 小时内 BEES 实际读取比对过的数据量）
- `scanf_total_ms` = 13,166,484 ms ≈ 3.66 小时（4 小时 uptime 里，真正花在扫描上的时间占比 ~91%）
- 有效吞吐 ≈ 697 GiB / 3.66 h ≈ **~190 GiB/小时**（单线程，`--thread-count 1`，`--loadavg-target 2.0`）

按此速率推算 3.8 TB（≈ 3891 GiB）全新数据：

```
3891 GiB / 190 GiB/h ≈ 20.5 小时
```

**给出 ~20-30 小时的区间**而不是单点数字，原因：

- 上面的基准数据来自"大部分已经被扫描过、hash 表却很小导致大量 evict 重算"的场景，实际吞吐可能因文件类型不同而变化——USB 迁移进来的个人数据如果是大量小文件（照片、文档），随机 I/O 占比更高，吞吐可能低于连续大文件场景。
- hash 表从 128KB 扩到 1GB 后，evict 频率大幅下降，理论上应该会比这次的基准更快，但同时是全新的空表，前期"填表"阶段的命中率会比较低，两个因素部分抵消。
- `--thread-count 1` 是当前的保守配置。DS920+ 有 4 核（`nproc` 实测 = 4），如果在重扫期间观察到 NAS 整体负载正常、其它服务不受影响，可以考虑临时把 `--thread-count` 调到 2 加速重扫（可选优化，见下节），扫完后再降回 1 做长期日常运行。

---

## 7. 改动分级：最小必要 vs 可选

### P0（最小必要，直接根治本次故障和复发风险）

1. 新 `start-bees.sh`（PID 自愈锁 + hash 表自愈校验）
2. `/etc/logrotate.d/bees-volume3`（根治崩溃触发源，不做这一步锁再怎么修也可能复发同类崩溃）
3. crontab 合并为一行、去掉失效的 `pgrep`

这三项互相配合：logrotate 修复减少"崩溃"这个事件本身发生的概率，PID 自愈锁保证"万一还是崩溃了"也能在 5 分钟内自动恢复而不是永久卡死，hash 表自愈保证"每次真正启动"都跑在正确的配置下。三者缺一，这次故障的某种变体大概率还会复发。

### P1（可选，锦上添花，不做也不影响止血）

- `bees-status.sh` + Hermes 定时巡检（提升可观测性，能在下次故障发生的几分钟内就发现，而不是像这次一样三天后才注意到）
- `--thread-count` 从 1 调到 2 加速本次 3.8T 重扫（有轻微 I/O 竞争风险，建议先用 1 跑一段时间确认稳定后再考虑）
- systemd unit 替代 crontab 管理 BEES 生命周期（`Restart=always` 天然具备进程级自愈，可以作为 crontab 看门狗之外的第二道保险；这台机器已确认跑着 systemd v219，具备可行性，但改动面更大，建议等 P0 稳定运行一段时间后再评估）

### 不需要改动

- `beescrawl.dat` 的处理逻辑本身不需要单独实现——它已经被吸收进 P0 的 hash 表自愈设计里（只在重建 hash 表时联动清空）
- 新数据去重策略不需要额外脚本——`--scan-mode 0` 连续模式本身就会自动捕获新数据，P0 修复让守护进程保持存活即可

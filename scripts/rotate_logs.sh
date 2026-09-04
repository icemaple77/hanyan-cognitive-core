#!/bin/bash
# HCC 日志轮转 —— copy + truncate,不能 rename。
#
# 为什么不用 newsyslog(2026-09-04,公子问"能不能彻底解决"时顺手做的):
#   /etc/newsyslog.d/ 要 sudo,而这台机器上的服务全是用户级 launchd agent,
#   为一件维护活引入 root 不值得。
#
# ⚠️ 为什么必须 copy+truncate 而不是 mv:
#   launchd 通过 StandardErrorPath / StandardOutPath **持有这两个文件的 fd**。
#   mv 掉之后 launchd 手里的 fd 仍指向那个已被改名的 inode,新日志会继续写进
#   归档文件里,而 gateway.err.log 永远是 0 字节 —— 看起来轮转成功了,其实
#   日志全进了旧文件。只有原地 truncate(: > file)能让那个 fd 从头开始写。
#   代价是 copy 和 truncate 之间的极小窗口可能丢几行,对日志可以接受。
#
# 触发条件按大小,不按时间:这些日志的增长完全取决于同步循环刷得多凶
#   (查过:sync_from_qmd 曾刷出 29 万行),按天轮转会在忙的那天涨到几百 MB。

set -u
LOGDIR="$(cd "$(dirname "$0")/.." && pwd)/logs"
ARCHIVE="$LOGDIR/archive"
MAX_BYTES=$((32 * 1024 * 1024))   # 单个日志超 32MB 就轮转
KEEP=3                            # 每个日志留最近 3 份压缩归档

mkdir -p "$ARCHIVE"

for f in "$LOGDIR"/*.log; do
    [ -f "$f" ] || continue
    size=$(stat -f%z "$f" 2>/dev/null || echo 0)
    [ "$size" -lt "$MAX_BYTES" ] && continue

    base=$(basename "$f" .log)
    stamp=$(date +%Y%m%d-%H%M%S)
    dest="$ARCHIVE/${base}-${stamp}.log"

    cp "$f" "$dest" && : > "$f" && gzip -f "$dest"
    echo "$(date '+%F %T') 轮转 ${base}.log ($(echo "$size" | awk '{printf "%.0fMB", $1/1048576}')) -> $(basename "$dest").gz"

    # 只留最近 KEEP 份
    ls -t "$ARCHIVE/${base}-"*.log.gz 2>/dev/null | tail -n +$((KEEP + 1)) | while read -r old; do
        rm -f "$old" && echo "$(date '+%F %T') 删除旧归档 $(basename "$old")"
    done
done

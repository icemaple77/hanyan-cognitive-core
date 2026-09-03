#!/usr/bin/env python3
"""
HCC SSE 事件流常驻监听器
订阅 http://100.66.103.69:8000/api/v1/events/stream
将记忆变更事件 (store/update/delete) 实时归档到本地日志,
并把它们变成 OpenClaw 侧可消费的"最近变更索引" + 缓存失效信号 (P3-2)。
"""
import collections
import json
import os
import sys
import time
import datetime
import urllib.request

STREAM_URL = os.environ.get("HCC_STREAM_URL", "http://100.66.103.69:8000/api/v1/events/stream")
LOG_DIR = os.path.expanduser("~/.openclaw/workspace/memory/hcc-events")
LOG_FILE = os.path.join(LOG_DIR, "hcc-events.log")
STATE_FILE = os.path.join(LOG_DIR, "last_event.json")
# P3-2: bounded "recent memory changes" index — index.js's crossAgentSearch /
# memory_search still hits HCC directly for actual content; this file is a
# cheap local tail other tooling (or a future OpenClaw hook) can read without
# a round-trip, and a record of what changed while this monitor was up.
MEMORY_CHANGES_FILE = os.path.join(LOG_DIR, "memory_changes.jsonl")
MEMORY_CHANGES_MAX_LINES = 2000  # trimmed in batches, see _trim_memory_changes
# P3-2: mtime-only signal — hcc-openclaw-plugin/index.js's turnContextCache
# stats this file and forces an early refresh if it's newer than the cache
# entry, instead of waiting out the turn-throttle after memory changed.
CACHE_INVALIDATE_MARKER = os.path.join(LOG_DIR, "cache_invalidate.marker")
MEMORY_EVENT_TYPES = {"memory.created", "memory.updated", "memory.deleted"}
RECONNECT_DELAY = 5  # 断线重连间隔（秒）
STREAM_TIMEOUT = 45  # 无数据超时（秒）：服务端 keep-alive 15s，3 倍阈值；超时视为僵尸连接，断开重连

# 幂等:同一事件(按 event_type+memory_id+timestamp 判重)在本次连接生命周期内
# 只处理一次——SSE 服务端本身不重放历史事件,这里只防御同一事件被重复读到的
# 边界情况(如底层 socket 缓冲区异常)。
_recent_seen = collections.deque(maxlen=500)
_recent_seen_set = set()

def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).astimezone().isoformat()

def ensure_dirs():
    os.makedirs(LOG_DIR, exist_ok=True)

def _seen_before(event_type, data):
    key = (event_type, data.get("memory_id"), data.get("timestamp"))
    if key in _recent_seen_set:
        return True
    if len(_recent_seen) == _recent_seen.maxlen:
        _recent_seen_set.discard(_recent_seen[0])
    _recent_seen.append(key)
    _recent_seen_set.add(key)
    return False

def _trim_memory_changes():
    """批量裁剪:超过上限的 1.5 倍才整体重写一次,避免每条事件都重写整个文件。"""
    try:
        with open(MEMORY_CHANGES_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except FileNotFoundError:
        return
    if len(lines) <= MEMORY_CHANGES_MAX_LINES * 1.5:
        return
    with open(MEMORY_CHANGES_FILE, "w", encoding="utf-8") as f:
        f.writelines(lines[-MEMORY_CHANGES_MAX_LINES:])

def _touch_invalidate_marker(ts):
    # 内容本身不重要,重要的是 mtime——index.js 只 stat() 这个文件。写入时间戳
    # 只是方便人工排查"上次失效信号是什么时候"。
    with open(CACHE_INVALIDATE_MARKER, "w", encoding="utf-8") as f:
        f.write(ts)

def handle_memory_change(event_type, data):
    """把 memory.created/updated/deleted 事件落成本地"最近变更索引",并触发
    OpenClaw 侧 turnContextCache 的失效信号。data 字段见
    gateway/api/events_routes.py::_format_sse (P3-2 之前只有 action/memory_id/
    timestamp/source,现在补了 user_id/agent_id/type/tags/importance/
    memory_source)。"""
    record = {
        "ts": now_iso(),
        "event": event_type,
        "memory_id": data.get("memory_id"),
        "action": data.get("action"),
        "user_id": data.get("user_id"),
        "agent_id": data.get("agent_id"),
        "type": data.get("type"),
        "tags": data.get("tags"),
        "importance": data.get("importance"),
        "memory_source": data.get("memory_source"),
    }
    with open(MEMORY_CHANGES_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    _trim_memory_changes()
    _touch_invalidate_marker(record["ts"])

def append_event(event_type, data):
    entry = {
        "ts": now_iso(),
        "event": event_type,
        "data": data,
    }
    line = json.dumps(entry, ensure_ascii=False)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(entry, f, ensure_ascii=False, indent=2)
    print(f"[{entry['ts']}] {event_type}: {json.dumps(data, ensure_ascii=False)[:200]}", flush=True)
    if event_type in MEMORY_EVENT_TYPES and not _seen_before(event_type, data):
        handle_memory_change(event_type, data)

def listen_once():
    """建立一次 SSE 连接，返回是否成功（成功则持续读到断开，失败返回 False）"""
    req = urllib.request.Request(STREAM_URL, headers={"Accept": "text/event-stream"})
    try:
        with urllib.request.urlopen(req, timeout=STREAM_TIMEOUT) as resp:
            print(f"[{now_iso()}] ✅ SSE 已连接: {STREAM_URL}", flush=True)
            event_name = None
            data_lines = []
            while True:
                raw = resp.readline()
                if not raw:
                    print(f"[{now_iso()}] ⚠️ 连接被服务器关闭", flush=True)
                    return True
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                if line.startswith("event:"):
                    event_name = line[6:].strip()
                elif line.startswith("data:"):
                    data_lines.append(line[5:].strip())
                elif line == "":
                    # 事件边界
                    if data_lines:
                        data_str = "\n".join(data_lines)
                        try:
                            data = json.loads(data_str)
                        except json.JSONDecodeError:
                            data = {"raw": data_str}
                        append_event(event_name or "message", data)
                    event_name = None
                    data_lines = []
                # 其他 (如 : keep-alive 注释) 忽略
    except Exception as e:
        print(f"[{now_iso()}] ❌ 连接异常: {e}", flush=True)
        return False

def main():
    ensure_dirs()
    print(f"[{now_iso()}] 🚀 HCC SSE 监听器启动 (日志: {LOG_FILE})", flush=True)
    # 启动时记录一条 heartbeat
    append_event("monitor_start", {"note": "hcc-sse-monitor started"})
    while True:
        ok = listen_once()
        delay = 1 if ok else RECONNECT_DELAY
        print(f"[{now_iso()}] 🔄 {RECONNECT_DELAY if not ok else 1}s 后重连...", flush=True)
        time.sleep(delay)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("👋 监听器退出", flush=True)
        sys.exit(0)

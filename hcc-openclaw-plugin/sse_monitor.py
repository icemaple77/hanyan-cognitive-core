#!/usr/bin/env python3
"""
HCC SSE 事件流常驻监听器
订阅 http://100.66.103.69:8000/api/v1/events/stream
将记忆变更事件 (store/update/delete) 实时归档到本地日志
"""
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
RECONNECT_DELAY = 5  # 断线重连间隔（秒）
STREAM_TIMEOUT = 45  # 无数据超时（秒）：服务端 keep-alive 15s，3 倍阈值；超时视为僵尸连接，断开重连

def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).astimezone().isoformat()

def ensure_dirs():
    os.makedirs(LOG_DIR, exist_ok=True)

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

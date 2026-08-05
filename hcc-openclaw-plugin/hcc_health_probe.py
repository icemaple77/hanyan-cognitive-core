#!/usr/bin/env python3
"""
HCC 健康探针 — 每 30s 检查 hcc 服务状态
- 探测 /api/v1/health，记录 up/down 状态到日志
- 检测"半死"状态（慢响应 > 3s 但没挂）
- 输出 /tmp/hcc_probe_state.json 供其他脚本判断
"""
import json
import os
import time
import datetime
import urllib.request

HEALTH_URL = os.environ.get("HCC_HEALTH_URL", "http://localhost:8000/api/v1/health")
STATE_FILE = "/tmp/hcc_probe_state.json"
LOG_FILE = os.path.expanduser("~/.openclaw/workspace/memory/hcc-events/probe.log")
INTERVAL = 30  # 秒
SLOW_THRESHOLD = 3.0  # 秒，超过视为"半死"

def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).astimezone().isoformat()

def log(msg):
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"[{now_iso()}] {msg}\n")

def write_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)

def probe_once():
    """返回 (ok, latency_ms, detail)"""
    start = time.monotonic()
    try:
        req = urllib.request.Request(HEALTH_URL, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            latency = (time.monotonic() - start) * 1000
            body = resp.read().decode("utf-8", errors="replace")[:200]
            return True, latency, body
    except Exception as e:
        latency = (time.monotonic() - start) * 1000
        return False, latency, str(e)

def main():
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    consecutive_down = 0
    consecutive_slow = 0
    while True:
        ok, latency, detail = probe_once()
        slow = latency > SLOW_THRESHOLD * 1000

        if ok and not slow:
            consecutive_down = 0
            consecutive_slow = 0
            status = "up"
        elif ok and slow:
            consecutive_slow += 1
            consecutive_down = 0
            status = "slow"
            log(f"⚠️ SLOW {latency:.0f}ms (x{consecutive_slow})")
        else:
            consecutive_down += 1
            consecutive_slow = 0
            status = "down"
            log(f"❌ DOWN {latency:.0f}ms (x{consecutive_down}) {detail[:120]}")

        state = {
            "ts": now_iso(),
            "status": status,
            "latency_ms": round(latency, 1),
            "consecutive_down": consecutive_down,
            "consecutive_slow": consecutive_slow,
            "detail": detail[:200],
        }
        write_state(state)
        time.sleep(INTERVAL)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("👋 探针退出")

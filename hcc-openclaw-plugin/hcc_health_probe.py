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
import smtplib
from email.mime.text import MIMEText
from email.header import Header

HEALTH_URL = os.environ.get("HCC_HEALTH_URL", "http://100.66.103.69:8000/api/v1/health")
STATE_FILE = "/tmp/hcc_probe_state.json"
LOG_FILE = os.path.expanduser("~/.openclaw/workspace/memory/hcc-events/probe.log")
INTERVAL = 30  # 秒
SLOW_THRESHOLD = 3.0  # 秒，超过视为"半死"

# 告警配置（去抖：同状态只告警一次，恢复后重置）
ALERT_AFTER_DOWN = 20  # 连续 DOWN 20 次 ≈ 10 分钟
ALERT_AFTER_SLOW = 40  # 连续 SLOW 40 次 ≈ 20 分钟
ALERT_EMAIL_TO = os.environ.get("HCC_ALERT_EMAIL_TO", "icemaple7@gmail.com")
ALERT_SMTP = "192.168.1.18:587"  # Stalwart 局域网 SMTP（mail.chenyun.org 公网端口未暴露）

def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).astimezone().isoformat()

def log(msg):
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"[{now_iso()}] {msg}\n")

def write_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)

def load_mail_creds():
    """从 serena-mail.env 读取邮箱凭据（不打印敏感值）"""
    env_path = os.path.expanduser("~/.hanyanos/secrets/keys/serena-mail.env")
    creds = {}
    try:
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    creds[k.strip()] = v.strip()
    except Exception as e:
        log(f"⚠️ 读取邮箱凭据失败: {e}")
    return creds


def send_alert(subject, body):
    """发送告警邮件到 ALERT_EMAIL_TO，失败仅记录不抛出"""
    creds = load_mail_creds()
    user = creds.get("SERENA_EMAIL", "")
    pwd = creds.get("SERENA_PASSWORD", "")
    if not user or not pwd:
        log("⚠️ 告警邮件未发送：缺少邮箱凭据")
        return
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = user
    msg["To"] = ALERT_EMAIL_TO
    try:
        host, port = ALERT_SMTP.split(":")
        with smtplib.SMTP(host, int(port), timeout=15) as s:
            s.starttls()
            s.login(user, pwd)
            s.send_message(msg)
        log(f"📧 告警邮件已发送: {subject}")
    except Exception as e:
        log(f"❌ 告警邮件发送失败: {e}")


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
    down_alerted = False
    slow_alerted = False
    while True:
        ok, latency, detail = probe_once()
        slow = latency > SLOW_THRESHOLD * 1000

        if ok and not slow:
            consecutive_down = 0
            consecutive_slow = 0
            down_alerted = False
            slow_alerted = False
            status = "up"
        elif ok and slow:
            consecutive_slow += 1
            consecutive_down = 0
            status = "slow"
            log(f"⚠️ SLOW {latency:.0f}ms (x{consecutive_slow})")
            if consecutive_slow >= ALERT_AFTER_SLOW and not slow_alerted:
                slow_alerted = True
                send_alert(
                    "⚠️ [HCC] 服务半死告警",
                    f"HCC 网关 {HEALTH_URL} 连续 {consecutive_slow} 次慢响应（>3s，约 {consecutive_slow * INTERVAL // 60} 分钟）。\n最新延迟: {latency:.0f}ms\n请检查 Mac mini 的 HCC 服务。",
                )
        else:
            consecutive_down += 1
            consecutive_slow = 0
            status = "down"
            log(f"❌ DOWN {latency:.0f}ms (x{consecutive_down}) {detail[:120]}")
            if consecutive_down >= ALERT_AFTER_DOWN and not down_alerted:
                down_alerted = True
                send_alert(
                    "🚨 [HCC] 服务宕机告警",
                    f"HCC 网关 {HEALTH_URL} 连续 {consecutive_down} 次探测失败（约 {consecutive_down * INTERVAL // 60} 分钟），服务不可用！\n错误: {detail[:200]}\n请尽快检查 Mac mini。",
                )

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

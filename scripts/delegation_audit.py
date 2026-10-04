#!/usr/bin/env python3
"""含烟的派活合规度量(只读)。

规则(~/.openclaw/workspace/AGENTS.md):排错/修复/部署类先派专员;自己连续跑到第 3 条
exec 就该停手派出去。这里从 main 的会话库里数:每天 exec 多少次、派了多少次活、
出现了多少段"连续 ≥3 条 exec 且中间没派活"(违规段)。只数工具名,不读内容。

用法: python3 scripts/delegation_audit.py [--days 7] [--json]
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import sqlite3
import sys
from pathlib import Path

DB = Path.home() / ".openclaw/agents/main/agent/openclaw-agent.sqlite"
EXEC = {"exec", "bash", "process"}
SPAWN = {"sessions_spawn"}


def tool_names(ev: dict) -> list[str]:
    msg = ev.get("message") or {}
    if msg.get("role") != "assistant":
        return []
    out = []
    for b in msg.get("content") or []:
        if isinstance(b, dict) and b.get("type") in ("toolCall", "tool_use", "tool_call"):
            out.append(str(b.get("name") or b.get("toolName") or ""))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=a.days)).isoformat()
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    days: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    run: dict[str, int] = collections.defaultdict(int)  # session -> 当前连续 exec 数
    for sid, raw in con.execute("select session_id, event_json from transcript_events order by session_id, seq"):
        try:
            ev = json.loads(raw)
        except Exception:
            continue
        ts = str(ev.get("timestamp") or "")
        if ts < since:
            continue
        role = (ev.get("message") or {}).get("role")
        if role == "user":
            run[sid] = 0  # 新一轮指令,重新计数
            continue
        day = dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d")
        for name in tool_names(ev):
            if name in SPAWN:
                days[day]["spawn"] += 1
                run[sid] = 0
            elif name in EXEC:
                days[day]["exec"] += 1
                run[sid] += 1
                if run[sid] == 3:
                    days[day]["violations"] += 1
            elif name:
                days[day]["other_tools"] += 1
    rows = [{"day": d, **{k: c.get(k, 0) for k in ("exec", "spawn", "violations", "other_tools")}} for d, c in sorted(days.items())]
    if a.json:
        print(json.dumps(rows, ensure_ascii=False))
    else:
        print(f"{'日期':<12}{'exec':>6}{'派活':>6}{'违规段':>8}{'其他工具':>8}")
        for r in rows:
            print(f"{r['day']:<12}{r['exec']:>6}{r['spawn']:>6}{r['violations']:>8}{r['other_tools']:>8}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

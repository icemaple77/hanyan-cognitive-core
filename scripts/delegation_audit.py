#!/usr/bin/env python3
"""含烟(main)会话里的"空转"度量(只读,只看工具名和代码指纹,不输出内容)。

2026-10-05 更正:这个脚本的第一版把 exec 调用全算成"自己跑命令",结论是错的。
main 当时处于 OpenClaw 的代码模式(Code Mode),exec 的参数是一段 JS(`code`),
用来在隐藏的工具目录里搜工具再调用——发消息、轮询、派活都走它,不是 shell 命令。

真正值得盯的是**同一段调用原样重复**:同一条发不出去的消息重发几十次、
同一个轮询做二十多遍。这里按天统计:
- calls      工具调用总数
- code_cells 其中代码模式的调用(关掉代码模式后应为 0)
- repeats    与同一会话里上一次调用**完全相同**的次数(空转)
- worst      当天重复最多的一段调用重复了多少次
- spawns     派活次数(直接调用 sessions_spawn,或代码里出现它)

用法: python3 scripts/delegation_audit.py [--days 7] [--json]
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

DB = Path.home() / ".openclaw/agents/main/agent/openclaw-agent.sqlite"


def calls_of(ev: dict) -> list[tuple[str, str, bool, bool]]:
    """→ [(工具名, 调用指纹, 是否代码模式, 是否派活)]"""
    msg = ev.get("message") or {}
    if msg.get("role") != "assistant":
        return []
    out = []
    for b in msg.get("content") or []:
        if not (isinstance(b, dict) and b.get("type") in ("toolCall", "tool_use", "tool_call")):
            continue
        name = str(b.get("name") or "")
        args = b.get("arguments") if "arguments" in b else b.get("input")
        args = args if isinstance(args, dict) else {}
        code = args.get("code")
        is_code = name == "exec" and isinstance(code, str)
        body = code if is_code else json.dumps(args, sort_keys=True, ensure_ascii=False)
        fp = hashlib.sha1(f"{name}|{body}".encode()).hexdigest()
        spawn = name == "sessions_spawn" or (is_code and "sessions_spawn" in code)
        out.append((name, fp, is_code, spawn))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=a.days)).isoformat()
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    days: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    per_fp: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    last: dict[str, str] = {}
    for sid, raw in con.execute("select session_id, event_json from transcript_events order by session_id, seq"):
        try:
            ev = json.loads(raw)
        except Exception:
            continue
        ts = str(ev.get("timestamp") or "")
        if ts < since:
            continue
        day = dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d")
        for _name, fp, is_code, spawn in calls_of(ev):
            c = days[day]
            c["calls"] += 1
            c["code_cells"] += is_code
            c["spawns"] += spawn
            if last.get(sid) == fp:
                c["repeats"] += 1
                per_fp[day][fp] += 1
            last[sid] = fp
    rows = []
    for d, c in sorted(days.items()):
        worst = max(per_fp[d].values(), default=0)
        rows.append({"day": d, "calls": c["calls"], "code_cells": c["code_cells"], "repeats": c["repeats"],
                     "worst": worst + 1 if worst else 0, "spawns": c["spawns"]})
    if a.json:
        print(json.dumps(rows, ensure_ascii=False))
    else:
        print(f"{'日期':<12}{'调用':>6}{'代码模式':>8}{'原样重复':>8}{'最长重复':>8}{'派活':>6}")
        for r in rows:
            print(f"{r['day']:<12}{r['calls']:>6}{r['code_cells']:>8}{r['repeats']:>8}{r['worst']:>8}{r['spawns']:>6}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

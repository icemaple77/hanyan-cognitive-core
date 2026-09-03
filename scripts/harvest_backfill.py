#!/usr/bin/env python3
"""一次性补收历史会话(收割器"首见跳尾"漏掉的文件)。

收割器在线只追增量:新文件首见即把水位设到 EOF,历史 .reset/.deleted 归档
永远不会自动倒灌。本脚本手动补这类窗口——复用在线解析器(同一 _store 约定,
prompt-free,source=harvester:<rt>,自动过 4b),不动水位、幂等靠相邻去重。

用法(项目根目录,venv python):
  .venv/bin/python3 scripts/harvest_backfill.py --since 2026-08-28 --until 2026-09-03
  .venv/bin/python3 scripts/harvest_backfill.py --since 2026-09-02 --dry-run
窗口为本地(Australia/Brisbane)日期,含 since 不含 until。

历史战绩:09-03 补 09-02 主会话 324 条、补 08-28→09-03 三方 3841 条
(标签 backfill-0828)。详见 docs/RUNTIME-CHANGES-2026-09-03.md。
"""
from __future__ import annotations

import argparse
import asyncio
import glob
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

from core.session_harvester import (
    HCC_BASE,
    USER_ID,
    _parse_claude,
    _parse_openclaw,
)

BRISBANE = timezone(timedelta(hours=10))


def _to_iso_utc(local_date: str) -> str:
    dt = datetime.strptime(local_date, "%Y-%m-%d").replace(tzinfo=BRISBANE)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def collect(since: str, until: str) -> list[tuple[str, str, str]]:
    lo, hi = _to_iso_utc(since), _to_iso_utc(until)

    def in_w(ts):
        return ts and lo <= ts < hi

    items: list[tuple[str, str, str]] = []
    oc = os.path.expanduser("~/.openclaw/agents/main/sessions/*jsonl*")
    for f in glob.glob(oc):
        if ".trajectory." in f or f.endswith(".lock"):
            continue
        last = None
        with open(f, encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if not in_w(obj.get("timestamp") or ""):
                    continue
                p = _parse_openclaw(obj)
                if not p:
                    continue
                c = f"{p[0]}: {p[1]}"
                if c == last:
                    continue
                last = c
                items.append(("openclaw", "openclaw", c))
    for f in glob.glob(os.path.expanduser("~/.claude/projects/*/*.jsonl")):
        last = None
        with open(f, encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if not in_w(obj.get("timestamp") or ""):
                    continue
                p = _parse_claude(obj)
                if not p:
                    continue
                c = f"{p[0]}: {p[1]}"
                if c == last:
                    continue
                last = c
                items.append(("claude-code", "claude", c))
    db = os.path.expanduser("~/.hermes/state.db")
    if os.path.exists(db):
        lo_s = datetime.strptime(lo, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
        hi_s = datetime.strptime(hi, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
        try:
            for role, content in conn.execute(
                "SELECT role, content FROM messages WHERE role IN ('user','assistant') "
                "AND content IS NOT NULL AND content != '' AND timestamp >= ? AND timestamp < ?",
                (lo_s, hi_s),
            ):
                items.append(("hermes", "hermes", f"{role}: {content}"))
        finally:
            conn.close()
    return items


async def replay(items, tag: str, conc: int = 5) -> tuple[int, int]:
    done = fail = 0
    async with httpx.AsyncClient() as client:
        sem = asyncio.Semaphore(conc)

        async def one(agent, name, content):
            nonlocal done, fail
            async with sem:
                try:
                    r = await client.post(
                        f"{HCC_BASE}/memory/store",
                        json={
                            "content": content[:8000], "user_id": USER_ID, "agent_id": agent,
                            "type": "conversation", "source": f"harvester:{name}",
                            "importance": 0.4, "tags": ["harvested", name, tag],
                        },
                        timeout=15,
                    )
                    if r.status_code < 400:
                        done += 1
                    else:
                        fail += 1
                except Exception:
                    fail += 1

        await asyncio.gather(*[one(*it) for it in items])
    return done, fail


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--since", required=True, help="本地日期 YYYY-MM-DD(含)")
    ap.add_argument("--until", help="本地日期 YYYY-MM-DD(不含,默认今天)")
    ap.add_argument("--tag", default=None, help="入库标签(默认 backfill-<since>)")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    until = a.until or datetime.now(BRISBANE).strftime("%Y-%m-%d")
    tag = a.tag or f"backfill-{a.since.replace('-', '')}"
    items = collect(a.since, until)
    print(f"窗口 {a.since}→{until}(本地),待重放 {len(items)} 条")
    if a.dry_run:
        for agent, _n, c in items[:5]:
            print(f"  [{agent}] {c[:80]}")
        print("(dry-run,未入库)")
        return
    done, fail = asyncio.run(replay(items, tag))
    print(f"完成 {done},失败 {fail},标签 {tag}")


if __name__ == "__main__":
    main()

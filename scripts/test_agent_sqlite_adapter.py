"""agent_sqlite 适配器离线验证(金丝雀 fixture,生产语义对齐版)。"""
import asyncio
import json
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path.home() / "workspace/projects/HCC"))
from core import session_harvester as sh

DB = "/tmp/canary-slim.sqlite"
STATE = Path("/tmp/test_harvester_state.json")
if STATE.exists():
    STATE.unlink()
sh.STATE_PATH = STATE
collected = []
AD = {"name": "openclaw", "agent_id": "openclaw", "kind": "agent_sqlite",
      "glob": DB, "jsonl_cutover": "/nonexistent/*.jsonl", "parse": sh._parse_openclaw}


async def main():
    # 预置: 让最热会话尾部3行进入12h时间窗 → 模拟"持续活跃会话"
    c = sqlite3.connect(DB)
    sid, mx = c.execute(
        "select session_id, max(seq) from transcript_events "
        "group by session_id order by max(created_at) desc limit 1").fetchone()
    c.execute("update transcript_events set created_at=? where session_id=? and seq>=?",
              (int(time.time() * 1000) - 60000, sid, mx - 3))
    c.commit()

    h = sh.SessionHarvester()

    async def fake_store(client, content, agent_id, name):
        collected.append(content)
    h._store = fake_store

    # 轮1: 首见 → 尾水位设定,0 收割
    n1 = await h._harvest_agent_sqlite(AD, None)
    nsess = len([k for k in h._state if k.startswith("agentdb:")])
    assert n1 == 0 and not collected and nsess >= 1, (n1, collected, nsess)
    print(f"轮1 首见跳尾 ✅ (时间窗内会话数: {nsess})")

    # 轮2: 追加 user/assistant/toolResult 三行 → 只收 2 条对话
    now = int(time.time() * 1000)
    evs = [
        json.dumps({"type": "message", "message": {"role": "user", "content": "适配器测试甲消息"}}),
        json.dumps({"type": "message", "message": {"role": "assistant", "content": "适配器测试乙消息"}}),
        json.dumps({"type": "message", "message": {"role": "toolResult", "content": "工具噪音不该入库"}}),
    ]
    mx = c.execute("select max(seq) from transcript_events where session_id=?", (sid,)).fetchone()[0]
    for i, ev in enumerate(evs):
        c.execute("insert into transcript_events values (?,?,?,?)", (sid, mx + 1 + i, ev, now + i))
    c.commit()
    n2 = await h._harvest_agent_sqlite(AD, None)
    assert n2 == 2 and collected[0].startswith("user: 适配器测试甲"), (n2, collected)
    assert collected[1].startswith("assistant: 适配器测试乙")
    print("轮2 增量收割 2 条、toolResult 拒收 ✅")

    # 轮3: 幂等
    n3 = await h._harvest_agent_sqlite(AD, None)
    assert n3 == 0 and len(collected) == 2, (n3, collected)
    print("轮3 幂等 ✅")

    # 轮4: 相邻同内容重复(上一条原样重写) → 指纹过滤
    c.execute("insert into transcript_events values (?,?,?,?)",
              (sid, mx + 4, evs[1], now + 9))
    c.commit()
    n4 = await h._harvest_agent_sqlite(AD, None)
    assert n4 == 0 and len(collected) == 2, (n4, collected)
    print("轮4 相邻重复过滤 ✅ (真重复=连续同内容;隔条重发属正常对话,不该滤)")

    # 轮5: 状态持久化往返——新实例读 state 文件后不重灌
    h._save_state()
    h2 = sh.SessionHarvester()
    h2._store = fake_store
    n5 = await h2._harvest_agent_sqlite(AD, None)
    assert n5 == 0 and len(collected) == 2, (n5, collected)
    print("轮5 水位落盘/重启不重灌 ✅")

    # 轮6: 互斥——7.x jsonl 还活着时让位(Mac 现状)
    AD6 = dict(AD, jsonl_cutover=str(Path.home() / ".openclaw/agents/*/sessions/*.jsonl"))
    assert len(list(Path.home().glob(".openclaw/agents/*/sessions/*.jsonl"))) > 0, "Mac 应有活 jsonl"
    n6 = await h2._harvest_agent_sqlite(AD6, None)
    assert n6 == 0, n6
    print("轮6 与 7.x file 适配器互斥让位 ✅")

    c.close()
    print("\n== 六轮全过 ==")


asyncio.run(main())

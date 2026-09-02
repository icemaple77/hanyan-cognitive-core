"""Session Harvester —— HCC 主动从各 runtime 的会话文件收割对话,入库过 4b。

设计(2026-09-03,公子需求):
- **Agent 无感**:纯读会话文件,不依赖任何插件钩子/回调;openclaw 怎么升级都不影响。
- **无论哪个 agent**:每个 runtime 一个适配器(会话文件位置 + 行解析器)。
- **实时**:main.py lifespan 里周期跑(默认 60s),增量收割。
- **每一条**:每条 user/assistant 消息单独入库(type=conversation)→ 发 MEMORY_CREATED
  → 4b 初筛(core/noise_filter_events)判 keep/discard。冗余交给夜间 dreaming 去重压缩。

增量水位:每个文件记已读到的字节偏移,持久化到 state 文件;每轮只读新增。首次见到
某文件从 **EOF** 起(不倒灌历史,避免把 66MB 老会话一次性灌爆);之后 append 的都收。
会话 GC 把旧文件改名加后缀 → glob 不再匹配,自然停读。
"""
from __future__ import annotations

import glob
import json
import logging
import os
from pathlib import Path

import httpx

logger = logging.getLogger("hcc.harvester")

USER_ID = os.environ.get("HCC_HARVEST_USER_ID", "michael")
HCC_BASE = os.environ.get("HCC_SELF_URL", "http://127.0.0.1:8000/api/v1")
STATE_PATH = Path(os.environ.get("HCC_HARVEST_STATE", str(Path.home() / ".hcc" / "harvester_state.json")))
MAX_TEXT = 8000  # 单条消息入库上限(极长的截断,防个别巨消息)


def _extract_text(content) -> str:
    """兼容两种消息体:content 是字符串,或 [{type:text,text:...}, ...] 块数组。"""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text" and b.get("text"):
                parts.append(b["text"])
            elif isinstance(b, str):
                parts.append(b)
        return "\n".join(parts).strip()
    return ""


def _parse_openclaw(obj: dict):
    # ~/.openclaw/agents/main/sessions/*.trajectory.jsonl:{"type":"message","message":{role,content}}
    if obj.get("type") != "message":
        return None
    m = obj.get("message") or {}
    role = m.get("role")
    if role not in ("user", "assistant"):
        return None
    text = _extract_text(m.get("content"))
    return (role, text) if text else None


def _parse_claude(obj: dict):
    # ~/.claude/projects/*/*.jsonl:{"type":"user"|"assistant","message":{role,content}}
    t = obj.get("type")
    if t not in ("user", "assistant"):
        return None
    m = obj.get("message") or {}
    role = m.get("role") or t
    if role not in ("user", "assistant"):
        return None
    text = _extract_text(m.get("content"))
    # 跳过纯工具/系统噪音:命令输出包裹、caveat 等交给 4b,但空文本直接跳
    return (role, text) if text else None


# 每个 runtime 一个适配器。加新 runtime 只需在这里加一行。
ADAPTERS = [
    {"name": "openclaw", "agent_id": "openclaw",
     "glob": str(Path.home() / ".openclaw/agents/main/sessions/*.jsonl"), "parse": _parse_openclaw},
    {"name": "claude", "agent_id": "claude-code",
     "glob": str(Path.home() / ".claude/projects/*/*.jsonl"), "parse": _parse_claude},
    # hermes:会话文件位置待定,定位后加一行即可
]


class SessionHarvester:
    def __init__(self) -> None:
        self._state: dict[str, int] = self._load_state()
        self._last: dict[str, str] = {}  # 文件→上一条内容指纹,去相邻重复

    def _load_state(self) -> dict[str, int]:
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_state(self) -> None:
        try:
            STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            STATE_PATH.write_text(json.dumps(self._state), encoding="utf-8")
        except Exception:
            logger.warning("harvester: 写 state 失败", exc_info=True)

    async def _store(self, client: httpx.AsyncClient, content: str, agent_id: str, name: str) -> None:
        try:
            await client.post(f"{HCC_BASE}/memory/store", json={
                "content": content[:MAX_TEXT], "user_id": USER_ID, "agent_id": agent_id,
                "type": "conversation", "source": f"harvester:{name}", "importance": 0.4,
                "tags": ["harvested", name],
            }, timeout=10)
        except Exception as e:
            logger.warning("harvester: 入库失败 %s: %s", name, e)

    async def harvest_once(self) -> int:
        """收割一轮所有 runtime 的新消息,返回本轮入库条数。"""
        stored = 0
        async with httpx.AsyncClient() as client:
            for ad in ADAPTERS:
                for f in glob.glob(ad["glob"]):
                    if f.endswith(".trajectory.jsonl"):
                        continue  # openclaw 的 runtime trace,不是干净对话,跳过
                    try:
                        size = os.path.getsize(f)
                    except OSError:
                        continue
                    off = self._state.get(f)
                    if off is None:            # 首见:从 EOF 起,不倒灌历史
                        self._state[f] = size
                        continue
                    if size < off:             # 文件被截断/轮转:从头再来
                        off = 0
                    if size <= off:
                        continue
                    try:
                        with open(f, "r", encoding="utf-8", errors="ignore") as fh:
                            fh.seek(off)
                            data = fh.read()
                            new_off = fh.tell()
                    except OSError:
                        continue
                    # 只处理完整行(最后一行可能没写完 → 回退到最后换行)
                    if not data.endswith("\n"):
                        cut = data.rfind("\n")
                        if cut == -1:
                            continue           # 一整行都还没写完,等下轮
                        new_off = off + len(data[:cut + 1].encode("utf-8"))
                        data = data[:cut + 1]
                    for line in data.split("\n"):
                        if not line.strip():
                            continue
                        try:
                            obj = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        parsed = ad["parse"](obj)
                        if not parsed:
                            continue
                        role, text = parsed
                        content = f"{role}: {text}"
                        if self._last.get(f) == content:   # 去相邻重复
                            continue
                        self._last[f] = content
                        await self._store(client, content, ad["agent_id"], ad["name"])
                        stored += 1
                    self._state[f] = new_off
        self._save_state()
        if stored:
            logger.info("harvester: 本轮收割 %d 条对话入库(过 4b)", stored)
        return stored

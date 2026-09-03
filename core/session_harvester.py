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
import sqlite3
import time
from pathlib import Path

import httpx

logger = logging.getLogger("hcc.harvester")
from core.config import core_settings  # 单一配置源(2026-09-03)

USER_ID = core_settings.harvest_user_id
HCC_BASE = core_settings.self_url
STATE_PATH = Path(core_settings.harvest_state)
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


# 每个 runtime 一个适配器。kind=file(默认,tail JSONL)或 sqlite(查库,按自增 id 增量)。
ADAPTERS = [
    {"name": "openclaw", "agent_id": "openclaw", "kind": "file",
     "glob": str(Path.home() / ".openclaw/agents/main/sessions/*.jsonl"), "parse": _parse_openclaw},
    # 8.x: 会话 jsonl 整体迁入 per-agent SQLite(openclaw-agent.sqlite.transcript_events)。
    # 与 file 适配器互斥:只要 sessions/*.jsonl 还活着(7.x)本适配器让位,绝不双读双灌。
    {"name": "openclaw", "agent_id": "openclaw", "kind": "agent_sqlite",
     "glob": str(Path.home() / ".openclaw/agents/*/agent/openclaw-agent.sqlite"),
     "jsonl_cutover": str(Path.home() / ".openclaw/agents/*/sessions/*.jsonl"),
     "parse": _parse_openclaw},
    {"name": "claude", "agent_id": "claude-code", "kind": "file",
     "glob": str(Path.home() / ".claude/projects/*/*.jsonl"), "parse": _parse_claude},
    # hermes 不留 JSONL 对话流,对话在 ~/.hermes/state.db 的 messages 表(id 自增)
    {"name": "hermes", "agent_id": "hermes", "kind": "sqlite",
     "db": str(Path.home() / ".hermes/state.db"), "table": "messages"},
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
        """收割一轮所有 runtime 的新消息,返回本轮入库条数。单个 runtime 失败不影响其它。"""
        stored = 0
        async with httpx.AsyncClient() as client:
            for ad in ADAPTERS:
                try:
                    if ad.get("kind") == "sqlite":
                        stored += await self._harvest_sqlite(ad, client)
                    elif ad.get("kind") == "agent_sqlite":
                        stored += await self._harvest_agent_sqlite(ad, client)
                    else:
                        stored += await self._harvest_files(ad, client)
                except Exception:  # noqa: BLE001 - 一个源坏了不拖垮整轮
                    logger.warning("harvester: %s 收割失败", ad.get("name"), exc_info=True)
        self._save_state()
        if stored:
            logger.info("harvester: 本轮收割 %d 条对话入库(过 4b)", stored)
        return stored

    async def _harvest_files(self, ad: dict, client: httpx.AsyncClient) -> int:
        """tail JSONL 文件源(openclaw/claude):字节水位增量。"""
        stored = 0
        for f in glob.glob(ad["glob"]):
            if f.endswith(".trajectory.jsonl"):
                continue  # openclaw runtime trace,非干净对话
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
            if not data.endswith("\n"):  # 最后一行可能没写完 → 回退到最后换行
                cut = data.rfind("\n")
                if cut == -1:
                    continue
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
        return stored

    async def _harvest_agent_sqlite(self, ad: dict, client: httpx.AsyncClient) -> int:
        """8.x per-agent 库源:transcript_events(session_id,seq,event_json)。
        event_json 与旧 jsonl 行同格式 → 复用 file 解析器。互斥:7.x jsonl 还活着就让位。"""
        if ad.get("jsonl_cutover") and glob.glob(ad["jsonl_cutover"]):
            return 0  # 7.x 时代:file 适配器负责,避免同一对话双份入库
        stored = 0
        for db in sorted(glob.glob(ad["glob"])):
            try:
                conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
            except sqlite3.Error:
                continue
            try:
                cur = conn.cursor()
                try:  # 8.2 未落地的库可能没这张表 → 跳过不报错
                    rows = cur.execute(
                        "SELECT session_id, seq, event_json FROM transcript_events "
                        "WHERE created_at > ? AND (event_json LIKE '%\"type\":\"message\"%' "
                        "OR event_json LIKE '%\"type\": \"message\"%') "
                        "ORDER BY session_id, seq LIMIT 5000",
                        (int(time.time() * 1000) - 12 * 3600 * 1000,)).fetchall()
                except sqlite3.OperationalError:
                    return 0
            finally:
                conn.close()
            cur_session = None
            for session_id, seq, event_json in rows:
                key = f"agentdb:{db}:{session_id}"
                if session_id != cur_session:      # 换会话:每会话水位
                    cur_session = session_id
                wm = self._state.get(key)
                if wm is None:                     # 首见会话:水位=该会话最大 seq,不倒灌
                    mx = max(s for sid, s, _ in rows if sid == session_id)
                    self._state[key] = mx
                    continue
                if seq <= wm:
                    continue
                try:
                    obj = json.loads(event_json)
                except json.JSONDecodeError:
                    self._state[key] = seq         # 坏行也推进水位,不卡死
                    continue
                parsed = ad["parse"](obj)
                if parsed:
                    role, text = parsed
                    content = f"{role}: {text}"
                    lkey = f"last:{db}:{session_id}"
                    if self._last.get(lkey) != content:  # 相邻重复(重试/回写产生)
                        await self._store(client, content, ad["agent_id"], ad["name"])
                        stored += 1
                    self._last[lkey] = content
                self._state[key] = seq
        return stored

    async def _harvest_sqlite(self, ad: dict, client: httpx.AsyncClient) -> int:
        """SQLite 库源(hermes):按自增 id 增量查 messages 表。只读打开,绝不写库。"""
        db, table = ad["db"], ad["table"]
        if not os.path.exists(db):
            return 0
        key = f"sqlite:{db}:{table}"
        stored = 0
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
        try:
            cur = conn.cursor()
            wm = self._state.get(key)
            if wm is None:  # 首见:水位=当前最大 id,不倒灌历史
                row = cur.execute(f"SELECT COALESCE(MAX(id), 0) FROM {table}").fetchone()
                self._state[key] = row[0] if row else 0
                return 0
            rows = cur.execute(
                f"SELECT id, role, content FROM {table} "
                "WHERE id > ? AND role IN ('user','assistant') "
                "AND content IS NOT NULL AND content != '' ORDER BY id ASC LIMIT 500",
                (wm,),
            ).fetchall()
            for mid, role, text in rows:
                text = (text or "").strip()
                if text:
                    await self._store(client, f"{role}: {text}", ad["agent_id"], ad["name"])
                    stored += 1
                self._state[key] = mid
        finally:
            conn.close()
        return stored

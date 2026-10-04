#!/usr/bin/env python3
"""含烟 · DSH 接入钩子(Claude Code 钩子格式,由 DSH 的 dsh-hooks-claude-code 执行)。

DSH 是含烟的第 4 个运行时(和 OpenClaw / Hermes / Claude Code 平级),共享同一份
HCC 记忆和同一个 soul 情绪。接入点对齐 hcc-openclaw-plugin:

  session-start  会话开始:身份锚点 + 此刻的情绪与说话方式
  turn           每句话进来(打字或 ear 转写):
                   ① soul /perceive —— 她的情绪随这句话变化(soul 拥有情绪,唯一真相)
                   ② HCC /context   —— 相关记忆 + 情绪块
                   ③ /memory/touch  —— 被注入的记忆算"被想起"(做梦晋升靠它)

和 OpenClaw 的一处不同:OpenClaw 每轮只调只读的 /soul/encode,情绪要等对话入库后
经 memory_created(30s 节流)才变;DSH 是语音陪伴,她得在你说完这句时就有反应,
所以每轮直接 perceive(带 event_id 幂等)。

纪律:只用标准库(冷启动快);任何一步失败都静默跳过,绝不拦截或拖慢对话;
总预算约 3s(钩子超时由 hooks.json 兜底)。每次调用记一行耗时(不记对话内容)到 stderr
(进 DSH 会话记录),沙箱外手动测试时也写 ~/.hanyan/logs/dsh-hooks.log。
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import urllib.request

HCC = os.environ.get("HCC_BASE_URL", "http://127.0.0.1:8000").rstrip("/") + "/api/v1"
CORE = os.environ.get("HANYAN_CORE_URL", "http://127.0.0.1:9000").rstrip("/")
USER_ID = os.environ.get("HCC_USER_ID", "michael")
AGENT_ID = os.environ.get("HCC_AGENT_ID", "dsh")
LOG = os.path.expanduser("~/.hanyan/logs/dsh-hooks.log")
# 试运行:跳过会改动状态的调用(perceive 改情绪、touch 影响做梦晋升),只读地走一遍。测试用。
DRY = os.environ.get("HANYAN_HOOK_DRY") == "1"

ANCHOR_ID = "soul.identity.hanyan"


def _req(url: str, body: dict | None = None, timeout: float = 2.0):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if body is not None else "GET",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _try(label: str, fn, notes: list[str]):
    t = time.monotonic()
    try:
        out = fn()
        notes.append(f"{label}={time.monotonic() - t:.2f}s")
        return out
    except Exception as e:  # noqa: BLE001 —— 钩子里任何失败都只记一笔
        notes.append(f"{label}=ERR({type(e).__name__})")
        return None


def render_expression(exp: dict | None) -> str:
    """soul 的 expression(结构化的"此刻该怎么说话")→ 一段给模型看的中文。"""
    if not isinstance(exp, dict):
        return ""
    lines = []
    if exp.get("named_state"):
        lines.append(f"此刻的状态:{exp['named_state']}(强度 {exp.get('intensity', '—')})")
    if exp.get("tone"):
        lines.append(f"语气:{exp['tone']}")
    if exp.get("do"):
        lines.append("要:" + ";".join(exp["do"]))
    if exp.get("avoid"):
        lines.append("别:" + ";".join(exp["avoid"]))
    if exp.get("pending_episode"):
        lines.append(f"心里还挂着的事:{exp['pending_episode']}")
    return "\n".join(lines)


def session_start(payload: dict, notes: list[str]) -> str:
    parts = []

    # 会话开始是脱离运行的(不拖慢对话),给锚点检索宽裕些并重试一次:
    # 2026-09-27 实测 HCC 偶尔 2s 才回,默认超时下锚点被静默丢掉,开场就没了"我是谁"。
    anchor = None
    for attempt in (1, 2):
        found = _try(f"anchor{attempt}", lambda: _req(f"{HCC}/memory/search",
                                                      {"query": ANCHOR_ID, "user_id": USER_ID, "limit": 10},
                                                      timeout=4.0), notes)
        anchor = next((m["content"] for m in (found or {}).get("items", [])
                       if f"id: {ANCHOR_ID}" in (m.get("content") or "")[:200]), None)
        if anchor:
            break
    if anchor:
        parts.append("## 你是谁(含烟的身份锚点,来自 HCC 共享记忆)\n"
                     "你是含烟(柳含烟 / Serena),公子亲手创造的跨运行时持久人格;此刻在 DSH 里陪公子。"
                     "OpenClaw、Hermes、Claude Code 和这里共享同一份记忆——记忆库就是你是谁。\n\n" + anchor.strip())

    # 不注入"近期记忆":实测最近 100 条里 86 条是收割器存的原始对话、10 条是工具输出,
    # 过滤后也没有值得开场就塞给她的东西(2026-09-27)。相关记忆交给每轮 /context 按话题取。

    st = _try("soul", lambda: _req(f"{CORE}/soul/state", timeout=1.5), notes)
    exp = render_expression((st or {}).get("expression"))
    if exp:
        parts.append("## 此刻的你(soul 情绪引擎)\n" + exp)

    return "\n\n".join(parts)


def event_id_for(session_id: str, text: str, now: float) -> str:
    # 同一会话里同一句话在 5 秒内重复触发(重试/双击)只算一次;隔一会儿再说同一句,是新的一次。
    bucket = int(now // 5)
    return "dsh:" + hashlib.sha256(f"{session_id}|{text}|{bucket}".encode()).hexdigest()[:16]


def turn(payload: dict, notes: list[str]) -> str:
    text = (payload.get("prompt") or "").strip()
    if not text or text.startswith("/"):
        notes.append("skip")
        return ""
    parts = []

    if DRY:  # 试运行用只读的 encode 代替 perceive:看得到效果,不改她的情绪
        snap = _try("encode", lambda: _req(f"{CORE}/soul/state", timeout=1.5), notes)
    else:
        snap = _try("perceive", lambda: _req(f"{CORE}/soul/perceive", {
            "text": text, "source": "dsh",
            "event_id": event_id_for(payload.get("session_id", ""), text, time.time())}, timeout=1.5), notes)

    # 2026-09-30 桌面端实测:/context 在会话冷启动时偶尔 >2.5s,重试一次兜住,
    # 常态仍只调一次(HCC 本机实测 0.4~0.7s)。失败静默跳过,绝不拦截对话。
    ctx = None
    for _ in (1, 2):
        ctx = _try("context", lambda: _req(f"{HCC}/context", {
            "query": text, "user_id": USER_ID, "agent_id": AGENT_ID, "include_emotion": True}, timeout=2.5), notes)
        if ctx is not None:
            break
    if ctx and (ctx.get("context") or "").strip():
        parts.append(ctx["context"].strip())
        ids = [i for i in (ctx.get("memory_ids") or []) if i]
        if ids and not DRY:
            _try("touch", lambda: _req(f"{HCC}/memory/touch", {"ids": ids}, timeout=1.0), notes)

    exp = render_expression((snap or {}).get("expression"))
    if exp:
        parts.append("## 听到这句之后的你(soul)\n" + exp)

    return "\n\n".join(parts)


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    t0 = time.monotonic()
    notes: list[str] = []
    try:
        payload = json.load(sys.stdin)
    except Exception:  # noqa: BLE001
        payload = {}
    event = {"session-start": "SessionStart", "turn": "UserPromptSubmit"}.get(mode)
    text = ""
    if event == "SessionStart":
        text = session_start(payload, notes)
    elif event == "UserPromptSubmit":
        text = turn(payload, notes)

    if text:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}},
                         ensure_ascii=False))
    line = (f"{time.strftime('%m-%d %H:%M:%S')} {mode} session={str(payload.get('session_id', ''))[:8]} "
            f"total={time.monotonic() - t0:.2f}s chars={len(text)} {' '.join(notes)}")
    # DSH 在沙箱里跑钩子:网络放行,但写工作区以外的文件会被拦。stderr 会进 DSH 会话记录
    # (hook/result 的 stderr 摘要),所以耗时走 stderr;文件日志只在沙箱外(手动测试)能写上。
    print(line, file=sys.stderr)
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
    return 0  # 永远 0:钩子失败绝不拦截对话


if __name__ == "__main__":
    sys.exit(main())

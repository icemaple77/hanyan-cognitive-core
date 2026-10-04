#!/usr/bin/env python3
"""每日记忆摘要(试跑版,默认只出文件、不写库)。

把某几天公子和含烟的对话片段(HCC conversation 类记忆)交给 umbrella 上的本地模型
(qwen-35b,经 compute 代理),提炼成三类:episodes(发生了什么)/ preferences(偏好)/
facts(稳定事实),每条带来源片段 id,便于追溯。

硬约束(写死,不可配置):
- 模型只能是 llama-umbrella/*(本机 compute :9190 → umbrella),没有任何云端回退。
  对话内容(含亲密内容)不出这台 Mac 与 umbrella 之间的局域网。
- stdout 只打印计数,绝不打印对话或摘要文本;结果写入 chmod 600 的文件。
- 失败(umbrella 叫不醒/超时/模型输出不是 JSON)只记计数,不抛、不重试到云端。
- 亲密内容用克制、不露骨的概括(prompt 里要求),不复述细节/原话。

用法:
  python3 scripts/daily_digest.py --days 3            # 试跑,输出到 ~/.hcc/digest-dryrun-*.json
  python3 scripts/daily_digest.py --selftest           # 用合成的非私密对话验证流程
"""

from __future__ import annotations

import argparse
import datetime as dt
import http.client
import json
import os
import re
import subprocess
import sys
import urllib.request
from collections import defaultdict
from pathlib import Path

HCC = os.environ.get("HCC_BASE_URL", "http://127.0.0.1:8000").rstrip("/") + "/api/v1"
USER_ID = "michael"
AGENT_ID = "openclaw"  # 采集器只收 main(含烟)的会话,都记在这个 agent_id 下
MODEL = "llama-umbrella/qwen-35b"
CHUNK_CHARS = 5000
LINE_CHARS = 400
MAX_PAGES = 60  # 100 条/页,最多翻 6000 条

assert MODEL.startswith("llama-umbrella/"), "摘要只允许走本地 umbrella 模型"

PROMPT = """你在整理公子(用户)与含烟(AI 伴侣)的对话记录。输入是按时间排列、带编号的对话片段。
只输出一个 JSON 对象,不要输出 JSON 以外的任何文字:
{{"episodes":[{{"text":"...","src":[编号]}}],"preferences":[{{"text":"...","src":[编号]}}],"facts":[{{"text":"...","src":[编号]}}]}}
规则:
1. episodes = 这段时间实际发生的事、聊过的主题、公子的情绪状态,每条不超过 50 字,最多 6 条。
2. preferences = 公子明确表达的喜好、习惯、不喜欢的东西,最多 5 条。
3. facts = 稳定的事实(人物、设备、计划、约定),最多 5 条。
4. 只写对话里确实出现的,不要推测;没有就给空数组。
5. 涉及亲密/私密的内容,只用克制、不露骨的概括(如"今晚很亲近、聊得温柔"),不要复述细节或原话。
6. 机器输出、工具日志、英文工作过程不要写。
7. 用中文,称"公子"和"含烟"。
8. src 填支撑这条的片段编号。

对话记录({date}):
{transcript}
"""

CJK = re.compile(r"[一-鿿]")


COMPUTE = ("127.0.0.1", 9190)  # HanyanOS compute 器官:唤醒/占用/关机 umbrella
HOLD_TIMEOUT_S = 15 * 60  # 排队 + 开机 + 清显存,最多等这么久


def _compute_json(method: str, path: str, body: dict | None = None) -> dict:
    c = http.client.HTTPConnection(*COMPUTE, timeout=20)
    c.request(method, path, json.dumps(body or {}), {"Content-Type": "application/json"})
    r = c.getresponse()
    data = json.loads(r.read() or b"{}")
    c.close()
    return data


class UmbrellaHold:
    """向 compute 申请 GPU 占用(流式),拿到 granted 才算 umbrella 可用;退出时释放。
    只有"是我们叫醒的"才在释放后请求关机(非强制:别人在用就关不掉)。"""

    def __enter__(self):
        self.was_off = _compute_json("GET", "/compute/status")["umbrella"]["state"] == "off"
        self.conn = http.client.HTTPConnection(*COMPUTE, timeout=HOLD_TIMEOUT_S)
        self.conn.request(
            "POST", "/compute/hold",
            json.dumps({"who": "daily-digest", "reason": "每日记忆摘要", "gpu": True, "uses": "llm", "exclusive": True}),
            {"Content-Type": "application/json"},
        )
        resp = self.conn.getresponse()
        self.granted = False
        while True:
            line = resp.readline()
            if not line:
                break
            try:
                st = json.loads(line).get("state")
            except Exception:
                continue
            if st == "granted":
                self.granted = True
                break
            if st == "error":
                break
        return self

    def __exit__(self, *exc):
        try:
            self.conn.close()  # 关连接 = 释放占用
        except Exception:
            pass
        if self.was_off and self.granted:
            try:
                _compute_json("POST", "/compute/shutdown", {"who": "daily-digest", "force": False})
            except Exception:
                pass  # 关不掉(别人在用)就交给 compute 的空闲宽限
        return False


def _post(path: str, body: dict) -> dict:
    req = urllib.request.Request(
        f"{HCC}{path}", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def _local(ts: str) -> dt.datetime:
    t = dt.datetime.fromisoformat(ts)
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)  # HCC 存 UTC 无后缀
    return t.astimezone()


def fetch_fragments(since: dt.datetime) -> list[dict]:
    out: list[dict] = []
    for page in range(MAX_PAGES):
        data = _post(
            "/memory/search",
            {"query": "", "user_id": USER_ID, "agent_id": AGENT_ID, "type": "conversation",
             "limit": 100, "offset": page * 100},
        )
        items = data.get("items") or []
        if not items:
            break
        for m in items:
            t = _local(m["created_at"])
            if t < since:
                return out  # 结果按时间倒序,后面都更旧
            out.append({"id": m["id"], "t": t, "text": (m.get("content") or "").strip()})
        if len(items) < 100:
            break
    return out


def chunks_for_day(frags: list[dict]) -> list[list[dict]]:
    frags = sorted(frags, key=lambda f: f["t"])
    chunks, cur, size = [], [], 0
    for f in frags:
        line = f["text"].replace("\n", " ")[:LINE_CHARS]
        if not line or not CJK.search(line):  # 只留含中文的行,去掉英文工作输出
            continue
        if size + len(line) > CHUNK_CHARS and cur:
            chunks.append(cur)
            cur, size = [], 0
        cur.append({"id": f["id"], "line": line, "t": f["t"]})
        size += len(line)
    if cur:
        chunks.append(cur)
    return chunks


RETRY_WAIT_S = 60
MAX_ATTEMPTS = 10  # 开机后 immich-ml 可能占着显存(无卸载口,只能等它自己 5 分钟 TTL),最多等约 10 分钟
RETRYABLE = ("exited prematurely", "Connection error", "EHOSTDOWN", "502", "503")


def _extract_text(stdout: str) -> str:
    try:
        obj = json.loads(stdout[stdout.index("{"):])
    except Exception:
        return stdout

    def walk(o):
        if isinstance(o, dict):
            if isinstance(o.get("text"), str) and o["text"].strip():
                return o["text"]
            for v in o.values():
                r = walk(v)
                if r:
                    return r
        elif isinstance(o, list):
            for v in o:
                r = walk(v)
                if r:
                    return r

    return walk(obj) or stdout


def ask_model(prompt: str) -> str | None:
    """走 openclaw 的 llama-umbrella provider(直连 umbrella)。没有任何云端回退。
    模型进程起不来(显存被占等)这类可恢复错误会等待后重试。"""
    import time

    for attempt in range(MAX_ATTEMPTS):
        try:
            p = subprocess.run(
                ["openclaw", "infer", "model", "run", "--model", MODEL, "--prompt", prompt, "--json"],
                capture_output=True, text=True, timeout=900,
            )
        except Exception:
            return None
        if p.returncode == 0 and "\"ok\": false" not in p.stdout[:200]:
            return _extract_text(p.stdout)
        err = (p.stdout or "") + (p.stderr or "")
        if attempt < MAX_ATTEMPTS - 1 and any(k in err for k in RETRYABLE):
            time.sleep(RETRY_WAIT_S)
            continue
        return None
    return None


def parse_items(raw: str | None) -> dict | None:
    if not raw:
        return None
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except Exception:
        return None
    return d if isinstance(d, dict) else None


def digest_day(date: str, chunks: list[list[dict]], stats: dict) -> list[dict]:
    items: list[dict] = []
    for ch in chunks:
        transcript = "\n".join(f"[{i}] {c['line']}" for i, c in enumerate(ch))
        raw = ask_model(PROMPT.format(date=date, transcript=transcript))
        d = parse_items(raw)
        stats["chunks"] += 1
        if d is None:
            stats["chunk_failed"] += 1
            # 仅自测(合成数据)才落原始输出用于排错;真实数据绝不落
            if os.environ.get("DIGEST_SELFTEST_RAW"):
                Path(os.environ["DIGEST_SELFTEST_RAW"]).write_text(str(raw))
            continue
        for kind in ("episodes", "preferences", "facts"):
            for it in d.get(kind) or []:
                text = str((it or {}).get("text") or "").strip()
                if not text:
                    continue
                src = [ch[i]["id"] for i in (it.get("src") or []) if isinstance(i, int) and 0 <= i < len(ch)]
                items.append({"date": date, "type": kind[:-1] if kind != "facts" else "fact",
                              "text": text, "sources": src})
                stats[kind] += 1
    return items


def selftest_fragments() -> list[dict]:
    base = dt.datetime.now().astimezone().replace(hour=9, minute=0, second=0, microsecond=0)
    lines = [
        "user: 今天去办公室把 NAS 的硬盘换了,吵死了",
        "assistant: 宝,换完了吗?别又弄到半夜",
        "user: 换完了,明天再迁数据。我不喜欢吃香菜,你记着",
        "assistant: 记着了,以后点餐都帮你备注不要香菜",
        "user: 周六我们去海边吧",
        "assistant: 好呀,说好了周六,你别忘了带外套",
    ]
    return [{"id": f"selftest-{i}", "t": base + dt.timedelta(minutes=i), "text": s} for i, s in enumerate(lines)]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=3)
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()

    if a.selftest:
        frags = selftest_fragments()
    else:
        since = (dt.datetime.now().astimezone() - dt.timedelta(days=a.days)).replace(hour=0, minute=0, second=0, microsecond=0)
        frags = fetch_fragments(since)

    by_day: dict[str, list[dict]] = defaultdict(list)
    for f in frags:
        by_day[f["t"].strftime("%Y-%m-%d")].append(f)

    stats = {"days": len(by_day), "fragments": len(frags), "chunks": 0, "chunk_failed": 0,
             "episodes": 0, "preferences": 0, "facts": 0, "umbrella": "n/a"}
    all_items: list[dict] = []
    if by_day:
        with UmbrellaHold() as hold:
            stats["umbrella"] = "granted" if hold.granted else "not-granted"
            if hold.granted:
                for date in sorted(by_day):
                    all_items += digest_day(date, chunks_for_day(by_day[date]), stats)

    out = Path(a.out or Path.home() / ".hcc" / f"digest-dryrun-{dt.datetime.now():%Y%m%d-%H%M}{'-selftest' if a.selftest else ''}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"generated": dt.datetime.now().isoformat(), "model": MODEL, "stats": stats, "items": all_items},
                              ensure_ascii=False, indent=2))
    os.chmod(out, 0o600)
    print(json.dumps({"out": str(out), **stats}, ensure_ascii=False))  # 只打印计数
    return 0


if __name__ == "__main__":
    sys.exit(main())

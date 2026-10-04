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
  python3 scripts/daily_digest.py --yesterday --write --backfill-days 2 --knowledge 30
      # 定时任务(每天 07:00),也是做梦的"摘要阶段":昨天的对话 → 事件/偏好/事实;
      # 再补 2 个没摘要过的历史日子;再把 Deep 挑出的记忆组和存量模板空壳写成真正的知识(最多 30 组)
  python3 scripts/daily_digest.py --from-file X --write  # 把一份试跑结果写库,不再调模型

写库:事件/偏好/事实三类(type=event/preference/fact,source=daily_digest,标签 digest、
digest:<日期>)。和库里已有的事实/偏好/事件近乎相同的不重复写。已写过的日期记在
~/.hcc/digest-state.json,重跑会跳过(--force 除外)。
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
MAX_PAGES = 200  # 100 条/页,最多翻 20000 条(补历史时要往回看几十天)

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
    for idx, ch in enumerate(chunks):
        transcript = "\n".join(f"[{i}] {c['line']}" for i, c in enumerate(ch))
        prompt = PROMPT.format(date=date, transcript=transcript)
        raw = ask_model(prompt)
        d = parse_items(raw)
        stats["chunks"] += 1
        if d is None:
            # 输出不是 JSON(或模型没回话):补跑一次,再不行才算这一块丢了
            stats["chunk_retried"] = stats.get("chunk_retried", 0) + 1
            raw = ask_model(prompt)
            d = parse_items(raw)
        if d is None:
            stats["chunk_failed"] += 1
            # 只记"哪天第几块",不记内容——让缺了什么看得见
            stats.setdefault("failed_chunks", []).append({"date": date, "chunk": idx})
            # 仅自测(合成数据)才落原始输出用于排错;真实数据绝不落
            if os.environ.get("DIGEST_SELFTEST_RAW"):
                Path(os.environ["DIGEST_SELFTEST_RAW"]).write_text(str(raw))
            continue
        for kind in ("episodes", "preferences", "facts"):
            entries = d.get(kind)
            if not isinstance(entries, list):
                continue
            for it in entries:
                # 模型偶尔把条目写成纯字符串而不是 {"text","src"}:都接受,字符串没有来源编号
                if isinstance(it, str):
                    text, raw_src = it.strip(), []
                elif isinstance(it, dict):
                    text, raw_src = str(it.get("text") or "").strip(), it.get("src") or []
                else:
                    continue
                if not text:
                    continue
                if not isinstance(raw_src, list):
                    raw_src = [raw_src]
                src = [ch[i]["id"] for i in raw_src if isinstance(i, int) and 0 <= i < len(ch)]
                items.append({"date": date, "type": {"episodes": "episode", "preferences": "preference", "facts": "fact"}[kind],
                              "text": text, "sources": src})
                stats[kind] += 1
    return items


KIND_TO_TYPE = {"episode": "event", "preference": "preference", "fact": "fact"}
KIND_IMPORTANCE = {"episode": 0.5, "preference": 0.7, "fact": 0.65}
DIGEST_TYPES = set(KIND_TO_TYPE.values())
DUP_DISTANCE = 0.08  # 余弦距离;已有的事实/偏好/事件里有这么近的就不重复写
STATE_PATH = Path.home() / ".hcc" / "digest-state.json"


def _is_duplicate(text: str) -> bool:
    """库里已有近乎相同的摘要条目(只和事实/偏好/事件比,不和原话比)。查不了就当不重复。"""
    try:
        data = _post("/memory/hybrid-search", {"query": text, "user_id": USER_ID, "limit": 5})
    except Exception:
        return False
    for it in data.get("items") or []:
        m, d = it.get("memory") or {}, it.get("vector_distance")
        if d is not None and d <= DUP_DISTANCE and (m.get("type") in DIGEST_TYPES or m.get("source") == "daily_digest"):
            return True
    return False


def write_items(items: list[dict], stats: dict) -> None:
    """把摘要条目写进 HCC。带 digest 标签,回滚 = 把带该标签的行软删。"""
    for k in ("written", "skipped_dup", "write_failed"):
        stats.setdefault(k, 0)
    for it in items:
        text = (it.get("text") or "").strip()
        kind = it.get("type")
        if len(text) < 6 or kind not in KIND_TO_TYPE:
            continue
        if _is_duplicate(text):
            stats["skipped_dup"] += 1
            continue
        try:
            _post("/memory/store", {
                "content": f"[{it['date']}] {text}", "summary": text,
                "user_id": USER_ID, "agent_id": AGENT_ID,
                "type": KIND_TO_TYPE[kind], "source": "daily_digest",
                "importance": KIND_IMPORTANCE[kind],
                # soul:perceived:这些话含烟当时已经感知过,别再让情绪引擎算一遍
                "tags": ["digest", f"digest:{it['date']}", "soul:perceived"],
            })
            stats["written"] += 1
        except Exception:
            stats["write_failed"] += 1


KNOWLEDGE_PROMPT = """下面是公子与含烟的几条相关记忆(同一个话题),请把它们提炼成**一条**长期知识。
只输出一个 JSON 对象,不要输出 JSON 以外的任何文字:
{{"title":"不超过 20 字的标题","points":["要点1","要点2"]}}
规则:
1. points 写这组记忆共同说明的结论、事实、决定或经验,每条不超过 60 字,最多 6 条;写结论,不要复述对话过程。
2. 只写记忆里确实出现的,不要推测。
3. 如果这组内容只是闲聊、工具输出或零散过程,提炼不出值得长期记住的东西,就输出 {{"title":"","points":[]}}。
4. 涉及亲密/私密的内容,只用克制、不露骨的概括,不要复述细节或原话。
5. 用中文,称"公子"和"含烟"。

相关记忆:
{members}
"""


def _get(path: str) -> dict:
    with urllib.request.urlopen(f"{HCC}{path}", timeout=30) as r:
        return json.load(r)


def summarize_knowledge(limit: int, stats: dict) -> None:
    """做梦的知识巩固:把 Deep 挑出的记忆组(和存量模板空壳)交给大模型写成真正的知识。"""
    stats.update(knowledge_groups=0, knowledge_written=0, knowledge_empty=0, knowledge_failed=0)
    try:
        pending = _get(f"/dream/pending-knowledge?limit={limit}")
    except Exception:
        stats["knowledge_failed"] = -1  # 网关没有这个接口/不可达
        return
    stats["knowledge_pending_total"] = pending.get("total_pending", 0)
    for g in pending.get("groups") or []:
        stats["knowledge_groups"] += 1
        members = g.get("members") or []
        body = {"key": g["key"], "existing_id": g.get("existing_id"), "member_ids": [m["id"] for m in members]}
        if len(members) >= 2:
            listing = "\n".join(f"- {m['text']}" for m in members)
            d = parse_items(ask_model(KNOWLEDGE_PROMPT.format(members=listing)))
            if d is None:
                stats["knowledge_failed"] += 1
                continue  # 模型没给出 JSON:这组留到下次,不动库里的东西
            points = [str(p).strip() for p in (d.get("points") or []) if isinstance(p, (str, int, float)) and str(p).strip()]
            if points:
                body["title"] = str(d.get("title") or "").strip()
                body["content"] = "\n".join(f"- {p}" for p in points)
        # 成员不足 2 条或模型判定提炼不出 → content 留空,网关会把空壳软删
        try:
            r = _post("/dream/knowledge", body)
            stats["knowledge_written" if r.get("action") in ("created", "updated") else "knowledge_empty"] += 1
        except Exception:
            stats["knowledge_failed"] += 1


def _load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text())
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2))
    os.chmod(STATE_PATH, 0o600)


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
    ap.add_argument("--write", action="store_true", help="把结果写进 HCC(默认只出文件)")
    ap.add_argument("--yesterday", action="store_true", help="只处理昨天(定时任务用)")
    ap.add_argument("--force", action="store_true", help="已写过的日期也重跑")
    ap.add_argument("--backfill-days", type=int, default=0,
                    help="除了本次的日期,再补最近 N 个还没摘要过的日子(配合 --yesterday --write,慢慢把历史补齐)")
    ap.add_argument("--knowledge", type=int, default=0,
                    help="做梦知识巩固:最多把 N 组记忆写成知识(Deep 新挑出的优先,其次是存量模板空壳)")
    ap.add_argument("--knowledge-only", action="store_true", help="只做知识巩固,不摘要任何日期")
    ap.add_argument("--from-file", default=None, help="不调模型,直接把一份试跑结果写库(需配合 --write)")
    a = ap.parse_args()

    if a.from_file:
        d = json.loads(Path(a.from_file).read_text())
        stats = {"status": "ok", "from_file": True}
        state = _load_state()
        today = dt.datetime.now().astimezone().strftime("%Y-%m-%d")  # 今天还没过完,留给明早的定时任务
        items = [it for it in d.get("items") or []
                 if it.get("date", today) < today and (a.force or it.get("date") not in state)]
        if a.write:
            write_items(items, stats)
            for date in sorted({it["date"] for it in items}):
                state[date] = {"at": dt.datetime.now().isoformat(), "status": "ok", "source": "from-file"}
            _save_state(state)
        print(json.dumps(stats, ensure_ascii=False))
        return 0

    if a.selftest:
        frags = selftest_fragments()
    else:
        days = max(a.days, 45) if a.backfill_days else a.days  # 补历史要往回多看一些
        since = (dt.datetime.now().astimezone() - dt.timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0)
        frags = fetch_fragments(since)

    by_day: dict[str, list[dict]] = defaultdict(list)
    for f in frags:
        by_day[f["t"].strftime("%Y-%m-%d")].append(f)

    state = _load_state()
    if a.knowledge_only:
        by_day = {}
    if a.yesterday:
        today = dt.datetime.now().astimezone().strftime("%Y-%m-%d")
        y = (dt.datetime.now().astimezone() - dt.timedelta(days=1)).strftime("%Y-%m-%d")
        # 昨天 + 最近 N 个还没摘要过的更早的日子(从近到远)
        older = [k for k in sorted(by_day, reverse=True) if k < y and state.get(k, {}).get("status") != "ok"]
        keep = {y, *older[: max(0, a.backfill_days)]}
        by_day = {k: v for k, v in by_day.items() if k in keep and k < today}
    if a.write and not a.force:
        by_day = {k: v for k, v in by_day.items() if state.get(k, {}).get("status") != "ok"}

    stats = {"days": len(by_day), "fragments": len(frags), "chunks": 0, "chunk_failed": 0,
             "episodes": 0, "preferences": 0, "facts": 0, "umbrella": "n/a"}
    all_items: list[dict] = []
    started = dt.datetime.now().astimezone()
    want_knowledge = a.knowledge > 0 and a.write and not a.selftest
    if by_day or want_knowledge:
        with UmbrellaHold() as hold:
            stats["umbrella"] = "granted" if hold.granted else "not-granted"
            if hold.granted:
                # 知识巩固先做:它短,而且是做梦的正事;补历史可能要跑几个小时
                if want_knowledge:
                    summarize_knowledge(a.knowledge, stats)
                for date in sorted(by_day, reverse=True):  # 从近到远
                    failed_before = stats["chunk_failed"]
                    items = digest_day(date, chunks_for_day(by_day[date]), stats)
                    all_items += items
                    if a.write and not a.selftest:
                        # 每天跑完立刻写库并记状态:长跑中途被打断,已完成的日子不白跑
                        write_items(items, stats)
                        state[date] = {"at": dt.datetime.now().isoformat(),
                                       "status": "partial" if stats["chunk_failed"] > failed_before else "ok"}
                        _save_state(state)

    # 一眼能看出"今天到底跑没跑":not-run(没拿到 umbrella)/ partial(有块丢了)/ ok / nothing-to-do
    if not by_day and not want_knowledge:
        stats["status"] = "nothing-to-do"
    elif stats["umbrella"] != "granted":
        stats["status"] = "not-run"
    elif stats["chunk_failed"]:
        stats["status"] = "partial"
    else:
        stats["status"] = "ok"

    if a.write and not a.selftest:
        try:  # 记成做梦的一个阶段,/dream/status 和梦境面板看得到
            _post("/dream/runs", {"phase": "digest", "started_at": started.isoformat(),
                                  "stats": {k: v for k, v in stats.items() if k != "failed_chunks"}})
        except Exception:
            pass

    out = Path(a.out or Path.home() / ".hcc" / f"digest-dryrun-{dt.datetime.now():%Y%m%d-%H%M}{'-selftest' if a.selftest else ''}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"generated": dt.datetime.now().isoformat(), "model": MODEL, "stats": stats, "items": all_items},
                              ensure_ascii=False, indent=2))
    os.chmod(out, 0o600)
    print(json.dumps({"out": str(out), **stats}, ensure_ascii=False))  # 只打印计数
    return 1 if stats["status"] == "not-run" else 0  # 没跑成用退出码说出来,定时任务才看得见


if __name__ == "__main__":
    sys.exit(main())

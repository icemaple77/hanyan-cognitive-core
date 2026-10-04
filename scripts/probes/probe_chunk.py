#!/usr/bin/env python3
"""阶段2 探针（只读）：整条记忆 vs 切块 max 余弦 对比，q08/q12/q13。

三段对比：
  (1) 现状：整条记忆(规范文本 content+summary) 一个向量 与 query 的余弦
  (2) 切块：300-500字/15%重叠 切块，逐块余弦取 max（= 切块理论上限）
  (3) 近重复风险：全库近邻 top10 内部近重复对；期望记忆的兄弟块是否会挤占 top5

判据：若 (2) 明显 > (1) 且 (2) 超过 top5 边界 → 切块有戏；否则停下。

只读数据库（SELECT only）。不写 memories/documents。
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import re
import sys

MAIN = "/Users/michael/workspace/projects/HCC"
WT = "/tmp/hcc-phase2"
QFILE = "/Users/michael/.openclaw/workspace/tasks/hcc-eval/queries.jsonl"
DSN = "postgresql://hcc:hcc@127.0.0.1:5432/hcc"
QIDS = ["q08", "q12", "q13"]

os.chdir(MAIN)
sys.path.insert(0, WT)

import asyncpg  # noqa: E402
from gateway.core.embeddings import embed_text, memory_embedding_text  # noqa: E402

CHUNK_SIZE = 400
OVERLAP = 0.15


def parse_vec(v):
    if v is None:
        return None
    if isinstance(v, str):
        return [float(x) for x in v.strip().strip("[]").split(",")]
    return [float(x) for x in v]


def cos(a, b):
    if a is None or b is None:
        return -1.0
    d = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return d / (na * nb) if na and nb else 0.0


def split_sentences(t: str) -> list[str]:
    parts = re.split(r"(?<=[。！？；!?;\n])", t)
    return [p for p in parts if p.strip()]


def chunk_text(t: str, size: int = CHUNK_SIZE, overlap: float = OVERLAP) -> list[str]:
    """按句子装块，目标 size 字、overlap 比例重叠。"""
    sents = split_sentences(t)
    ov = int(size * overlap)
    chunks: list[str] = []
    cur = ""
    for s in sents:
        if len(cur) + len(s) > size and cur:
            chunks.append(cur)
            cur = cur[-ov:] + s if ov else s
        else:
            cur += s
    if cur.strip():
        chunks.append(cur)
    return chunks


async def global_nn(conn, qvec, scope: str | None, limit: int = 10):
    """全库 whole-memory 近邻（返回 [(id, cos, user_id)]）。scope=None 不过滤。"""
    S = "[" + ",".join(f"{x:.6f}" for x in qvec) + "]"
    if scope is None:
        rows = await conn.fetch(
            """select id, user_id, 1-(embedding <=> $1::vector) as cos from memories
               where status='active' and embedding is not null
               order by embedding <=> $1::vector limit $2""", S, limit)
    else:
        rows = await conn.fetch(
            """select id, user_id, 1-(embedding <=> $1::vector) as cos from memories
               where status='active' and embedding is not null and user_id=$2
               order by embedding <=> $1::vector limit $3""", S, scope, limit)
    return [(r["id"], float(r["cos"]), r["user_id"]) for r in rows]


async def nth_boundary(conn, qvec, scope: str | None, n: int):
    """第 n 名的余弦（top-n 边界）。"""
    rows = await global_nn(conn, qvec, scope, n)
    return rows[n - 1][1] if len(rows) >= n else None


async def main():
    qs = {json.loads(l)["id"]: json.loads(l)
          for l in open(QFILE, encoding="utf-8") if l.strip()}
    conn = await asyncpg.connect(DSN)

    for qid in QIDS:
        q = qs[qid]
        exp = [e["memory_id"] for e in q["expected"]]
        print("=" * 78)
        print(f"{qid}: {q['query']}")
        qvec = await asyncio.to_thread(embed_text, q["query"], is_query=True)

        # 当前 top5 边界（scope=michael，与线上主口径一致）
        b5 = await nth_boundary(conn, qvec, "michael", 5)
        b1 = await nth_boundary(conn, qvec, "michael", 1)
        print(f"  [参照] 作用域=michael 全库 whole-memory: top1 cos={b1:.4f}  top5边界 cos={b5:.4f}")

        for eid in exp:
            r = await conn.fetchrow(
                "select id,user_id,content,coalesce(summary,'') as summary,embedding,status,type,importance "
                "from memories where id=$1", eid)
            if r is None:
                print(f"  {eid[:8]} ← 不存在"); continue
            content, summary = r["content"] or "", r["summary"] or ""
            canon = memory_embedding_text(content, summary)
            print(f"\n  ── 期望 {eid[:8]} user={r['user_id']} status={r['status']} "
                  f"type={r['type']} len(content)={len(content)} len(summary)={len(summary)}")

            # (1) 现状：库内向量
            stored = parse_vec(r["embedding"])
            c_stored = cos(stored, qvec)
            # (1b) 规范文本重嵌整条
            whole = await asyncio.to_thread(embed_text, canon)
            c_whole = cos(whole, qvec)
            print(f"  (1) 现状整条: 库内向量 cos={c_stored:.4f} | 规范重嵌 cos={c_whole:.4f}")

            # (2) 切块 max
            chunks = chunk_text(canon)
            # 摘要单独成块
            chunk_labels = [f"body#{i+1}" for i in range(len(chunks))]
            if summary.strip():
                chunks = chunks + [summary.strip()]
                chunk_labels = chunk_labels + ["summary"]
            coss = []
            for i, ch in enumerate(chunks):
                cv = await asyncio.to_thread(embed_text, ch)
                coss.append((chunk_labels[i], len(ch), cos(cv, qvec)))
            c_max = max(c[2] for c in coss)
            print(f"  (2) 切块 {len(chunks)} 块 (size={CHUNK_SIZE},ov={OVERLAP:.0%}) max cos={c_max:.4f}")
            for lab, ln, c in sorted(coss, key=lambda x: -x[2]):
                star = "★" if c > b5 else " "
                print(f"        {lab:<10} len={ln:<5} cos={c:.4f} {star}")
            delta = c_max - c_whole
            print(f"  Δ = (2)-(1) = {delta:+.4f}   "
                  f"{'✅ 超 top5 边界 → 能救回' if c_max > b5 else '❌ 仍够不到 top5 边界'}")

            # (3a) 全库近邻 top10 内部近重复对 + 与期望的近重复
            for scope in ("michael", None):
                nn = await global_nn(conn, qvec, scope, 10)
                vecs = {}
                for rid, c, _u in nn:
                    vecs[rid] = parse_vec(await conn.fetchval(
                        "select embedding from memories where id=$1", rid))
                pairs = sum(1 for i in range(len(nn)) for j in range(i + 1, len(nn))
                            if cos(vecs[nn[i][0]], vecs[nn[j][0]]) >= 0.95)
                expv = stored
                exp_clones = sum(1 for rid, c, _u in nn
                                 if rid != eid and cos(vecs[rid], expv) >= 0.95)
                exphits = sum(1 for rid, c, _u in nn if rid in exp)
                print(f"  (3) 全库top10 scope={'michael' if scope else 'ALL'}: "
                      f"内部≥0.95对={pairs} 与期望≥0.95的克隆={exp_clones} 期望在top10={'YES' if exphits else 'no'}")
                print("       " + ", ".join(f"{rid[:8]}({c:.3f},{'exp' if rid in exp else '--'})"
                                            for rid, c, _u in nn))

            # (3b) 兄弟块挤占 top5 风险：多少块 cos > top5 边界
            sib = sum(1 for _l, _n, c in coss if c > b5)
            print(f"  (3b) 期望记忆自身 {sib} 个块 cos > top5 边界 → "
                  f"{'会挤占'+str(sib)+'个 top5 名额' if sib else '不会挤占'}")

    await conn.close()


if __name__ == "__main__":
    asyncio.run(main())

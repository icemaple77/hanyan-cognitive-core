#!/usr/bin/env python3
"""阶段2 探针·拥挤/重复 + 原文型vs摘要型（只读）。

A. 库级重复：最长记忆之间的近重复簇（同一原文存多次的量化）
B. 每查询（scope=all）：
   - 期望记忆分类（原文型 / 摘要型 / 短知识型）+ 长度
   - whole 排名 vs max-chunk 排名（父记忆级）
   - 块级 top-12：同一父的兄弟块数、同原文副本数（拥挤风险）
只读数据库，不改任何行。
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
POOL = int(os.environ.get("PROBE_POOL", "150"))
CHUNK_SIZE, OVERLAP = 400, 0.15
RAW_TYPES = ("chatroom", "session_summary", "conversation", "tool_result", "chatroom_message")

os.chdir(MAIN)
sys.path.insert(0, WT)
import asyncpg  # noqa: E402
from gateway.core.embeddings import embed_text, memory_embedding_text  # noqa: E402


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


def split_sentences(t):
    return [p for p in re.split(r"(?<=[。！？；!?;\n])", t) if p.strip()]


def chunk_text(t, size=CHUNK_SIZE, overlap=OVERLAP):
    sents = split_sentences(t)
    ov = int(size * overlap)
    chunks, cur = [], ""
    for s in sents:
        if len(cur) + len(s) > size and cur:
            chunks.append(cur)
            cur = cur[-ov:] + s if ov else s
        else:
            cur += s
    if cur.strip():
        chunks.append(cur)
    return chunks


async def part_a(conn):
    print("=" * 78)
    print("A. 库级近重复：最长 200 条记忆内部 ≥0.95 的副本簇（同一原文存多次）")
    rows = await conn.fetch(
        """select id, type, source, length(content) as l, embedding
           from memories where status='active' and embedding is not null
             and length(content) > 800
           order by length(content) desc limit 200""")
    ids = [r["id"] for r in rows]
    vecs = [parse_vec(r["embedding"]) for r in rows]
    n = len(rows)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    pairs = 0
    for i in range(n):
        for j in range(i + 1, n):
            if cos(vecs[i], vecs[j]) >= 0.95:
                pairs += 1
                a, b = find(i), find(j)
                if a != b:
                    parent[a] = b
    from collections import Counter
    clusters = Counter(find(i) for i in range(n))
    multi = {k: v for k, v in clusters.items() if v > 1}
    print(f"  样本={n} 条(>800字)  ≥0.95 近重复对={pairs}  近重复簇={len(multi)}")
    print(f"  最大簇={max(clusters.values()) if clusters else 0}  "
          f"落在多成员簇的条数={sum(multi.values())}")
    for k, v in sorted(multi.items(), key=lambda x: -x[1])[:5]:
        members = [rows[i] for i in range(n) if find(i) == k]
        lens = sorted((m["l"] for m in members), reverse=True)
        print(f"    簇 size={v} 长度={lens[:6]} type={Counter(m['type'] for m in members).most_common(3)}")


async def part_b(conn):
    qs = {json.loads(l)["id"]: json.loads(l)
          for l in open(QFILE, encoding="utf-8") if l.strip()}
    raw_embeds = 0
    for qid in QIDS:
        q = qs[qid]
        exp = {e["memory_id"] for e in q["expected"]}
        qvec = await asyncio.to_thread(embed_text, q["query"], is_query=True)
        raw_embeds += 1
        S = "[" + ",".join(f"{x:.6f}" for x in qvec) + "]"
        rows = await conn.fetch(
            """select id, type, source, content, coalesce(summary,'') as summary, embedding
               from memories where status='active' and embedding is not null
               order by embedding <=> $1::vector limit $2""", S, POOL)
        print("=" * 78)
        print(f"B. {qid}: {q['query']}  (scope=ALL, 池={len(rows)})")
        # 期望分类
        for eid in exp:
            er = next((r for r in rows if r["id"] == eid), None)
            if er is None:
                er = await conn.fetchrow(
                    "select id,type,source,content,coalesce(summary,'') as summary,embedding "
                    "from memories where id=$1", eid)
            if er:
                kind = "原文型" if er["type"] in RAW_TYPES else "摘要/短知识型"
                print(f"   期望 {eid[:8]} type={er['type']}({kind}) source={er['source']} "
                      f"len(content)={len(er['content'] or '')} len(summary)={len(er['summary'] or '')}")

        whole_rank, chunk_rank, chunk_level = [], [], []
        for r in rows:
            canon = memory_embedding_text(r["content"], r["summary"])
            wv = await asyncio.to_thread(embed_text, canon)
            raw_embeds += 1
            chs = chunk_text(canon)
            if r["summary"] and r["summary"].strip():
                chs = chs + [r["summary"].strip()]
            best = -1.0
            for ci, c in enumerate(chs):
                cv = await asyncio.to_thread(embed_text, c)
                raw_embeds += 1
                cc = cos(cv, qvec)
                chunk_level.append((r["id"], ci, cc, len(chs)))
                best = max(best, cc)
            whole_rank.append((r["id"], cos(wv, qvec)))
            chunk_rank.append((r["id"], best))

        whole_rank.sort(key=lambda x: -x[1])
        chunk_rank.sort(key=lambda x: -x[1])
        for label, rank in (("整条 whole", whole_rank), ("全库切块 max-chunk", chunk_rank)):
            top5 = rank[:5]
            pos = next((i + 1 for i, (i2, _) in enumerate(rank) if i2 in exp), None)
            print(f"   {label:<20} 期望名次={pos} {'✅救回' if pos and pos <= 5 else '❌仍漏'} "
                  f"top5边界={round(rank[4][1],4)}")
            print("      top5=" + ", ".join(f"{i[:8]}{'★' if i in exp else ''}({round(c,4)})"
                                            for i, c in top5))

        # 块级 top-12 拥挤
        chunk_level.sort(key=lambda x: -x[2])
        t12 = chunk_level[:12]
        parents = {}
        for pid, ci, cc, nc in t12:
            parents.setdefault(pid, []).append(ci)
        sib_pairs = sum(len(v) - 1 for v in parents.values() if len(v) > 1)
        # 副本：top12 里不同父但互为 ≥0.95
        t12_vecs = {}
        for pid, ci, cc, nc in t12:
            if pid not in t12_vecs:
                t12_vecs[pid] = parse_vec(await conn.fetchval(
                    "select embedding from memories where id=$1", pid))
        pids = list({p for p, _, _, _ in t12})
        copies = 0
        for i in range(len(pids)):
            for j in range(i + 1, len(pids)):
                if cos(t12_vecs[pids[i]], t12_vecs[pids[j]]) >= 0.95:
                    copies += 1
        print(f"   块级 top12: 涉及父记忆={len(pids)} 同一父的兄弟块对={sib_pairs} "
              f"不同父互为副本对={copies}")
        print("     块级 top12 明细: " + ", ".join(
            f"{pid[:8]}#{ci}({round(cc,3)}){'★' if pid in exp else ''}" for pid, ci, cc, nc in t12))
    print(f"\n[探针] 累计嵌入调用 ≈ {raw_embeds}")


async def main():
    conn = await asyncpg.connect(DSN)
    await part_a(conn)
    await part_b(conn)
    await conn.close()


if __name__ == "__main__":
    asyncio.run(main())

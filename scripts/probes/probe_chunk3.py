#!/usr/bin/env python3
"""阶段2 探针·精化（只读）：把「竞争者也切块」后的公平边界算出来。

上一版只切了期望记忆，竞争者仍是整条 → top5 边界被低估。本版：
  1. 取全库 whole-memory top-100 近邻（scope=michael）
  2. 对**每一块**（期望+竞争者）都切块、逐块嵌入、取每条记忆的 max-chunk 余弦
  3. 按 max-chunk 余弦重排 → 模拟「全库切块检索」的 recall@5 与真实边界
  4. 与「仅整条」排名对照，给出 q08/q12/q13 是否真的被救回

只读数据库。
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
POOL = int(os.environ.get("PROBE_POOL","100"))
SCOPE = os.environ.get("PROBE_SCOPE","michael")
CHUNK_SIZE, OVERLAP = 400, 0.15

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


async def main():
    qs = {json.loads(l)["id"]: json.loads(l)
          for l in open(QFILE, encoding="utf-8") if l.strip()}
    conn = await asyncpg.connect(DSN)
    total_embeds = 0

    for qid in QIDS:
        q = qs[qid]
        exp = {e["memory_id"] for e in q["expected"]}
        qvec = await asyncio.to_thread(embed_text, q["query"], is_query=True)
        total_embeds += 1
        S = "[" + ",".join(f"{x:.6f}" for x in qvec) + "]"

        rows = await conn.fetch(
            """select id, content, coalesce(summary,'') as summary
               from memories where status='active' and embedding is not null
                 and ($3='all' or user_id=$3) order by embedding <=> $1::vector limit $2""",
            S, POOL, SCOPE)
        print("=" * 78)
        print(f"{qid}: {q['query']}   (池={len(rows)})")

        whole_rank, chunk_rank = [], []
        for r in rows:
            canon = memory_embedding_text(r["content"], r["summary"])
            whole = await asyncio.to_thread(embed_text, canon)  # 整条重嵌（口径一致）
            total_embeds += 1
            chs = chunk_text(canon)
            if r["summary"] and r["summary"].strip():
                chs = chs + [r["summary"].strip()]
            cmax = -1.0
            for c in chs:
                cmax = max(cmax, cos(await asyncio.to_thread(embed_text, c), qvec))
            total_embeds += len(chs)
            whole_rank.append((r["id"], cos(whole, qvec)))
            chunk_rank.append((r["id"], cmax))

        whole_rank.sort(key=lambda x: -x[1])
        chunk_rank.sort(key=lambda x: -x[1])

        def show(label, ranking, is_chunk):
            top5 = ranking[:5]
            exp_pos = next((i + 1 for i, (i2, _) in enumerate(ranking) if i2 in exp), None)
            hit = exp_pos is not None and exp_pos <= 5
            ids = [(i[:8] + ("★" if i in exp else ""), round(c, 4)) for i, c in top5]
            print(f"  {label:<22} 期望名次={exp_pos}  {'✅ 救回' if hit else '❌ 仍漏'}")
            print(f"      top5={ids}")
            print(f"      top5 边界 cos={round(ranking[4][1], 4)}")
            return exp_pos, hit

        show("整条 whole（对照）", whole_rank, False)
        show("全库切块 max-chunk", chunk_rank, True)

    print(f"\n[探针精化] 本次累计嵌入调用 ≈ {total_embeds} 次")
    await conn.close()


if __name__ == "__main__":
    asyncio.run(main())

#!/usr/bin/env python3
"""阶段2 探针·原文型 vs 摘要型：稀释效应机制验证（只读，合成 query）。

评测集 q08/q12/q13 的期望记忆都是「摘要/短知识型」，没有一条是原文型长记忆。
故另取 3 条代表「原文型」长记忆，用其**中段句子**做 query（模拟「query 只命中其中一句」），
对比 整条余弦 vs 切块 max 余弦 —— 验证稀释机制是否真实存在。
同时给出该原文在库内的副本数（≥0.95）。
"""
from __future__ import annotations

import asyncio
import math
import os
import re
import sys

MAIN = "/Users/michael/workspace/projects/HCC"
WT = "/tmp/hcc-phase2"
DSN = "postgresql://hcc:hcc@127.0.0.1:5432/hcc"
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
    conn = await asyncpg.connect(DSN)
    # 取原文型长记忆代表：最长 chatroom / 最长 conversation / 最长 session_summary
    picks = []
    for typ in ("chatroom", "conversation", "session_summary"):
        r = await conn.fetchrow(
            "select id,type,source,content,embedding from memories where status='active' "
            "and embedding is not null and type=$1 order by length(content) desc limit 1", typ)
        if r:
            picks.append(r)
    for r in picks:
        canon = memory_embedding_text(r["content"], "")
        # query = 中段一个 30-60 字的句子（模拟命中其中一句）
        sents = [s.strip() for s in split_sentences(r["content"] or "") if 25 <= len(s.strip()) <= 80]
        mid = sents[len(sents) // 2] if sents else (r["content"] or "")[:60]
        wv = await asyncio.to_thread(embed_text, canon)
        qv = await asyncio.to_thread(embed_text, mid, is_query=True)
        chs = chunk_text(canon)
        cms = []
        for c in chs:
            cv = await asyncio.to_thread(embed_text, c)
            cms.append(cos(cv, qv))
        cmax = max(cms) if cms else -1
        # 副本数
        S = "[" + ",".join(f"{x:.6f}" for x in parse_vec(r["embedding"])) + "]"
        copies = await conn.fetchval(
            "select count(*)-1 from memories where status='active' and embedding is not null "
            "and id<>$2 and (embedding <=> $1::vector) < 0.05", S, r["id"])
        print("=" * 74)
        print(f"{r['type']} len={len(r['content'] or '')} source={r['source']} id={r['id'][:8]}")
        print(f"  query(中段句)={mid[:50]!r}")
        print(f"  整条余弦={cos(wv, qv):.4f}   切块({len(chs)}块) max={cmax:.4f}   Δ={cmax-cos(wv, qv):+.4f}")
        print(f"  库内 ≥0.95 副本数={copies}")
    await conn.close()


if __name__ == "__main__":
    asyncio.run(main())

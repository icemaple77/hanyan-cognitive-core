"""向量维度启动自检 —— 让"换模型漏迁移"这类事故不能再静默。

2026-08-29 事故复盘(commit f75ab97):嵌入模型从 qwen3-embedding:0.6b(1024)
换成更轻量的 bge-base-zh-v1.5(768)。配套迁移是**手工在线上执行**的,只覆盖了
memories 的 4673 条,**漏掉 documents 的 245 行**;同时 .env 在 gitignore 里、
models 的 EMBEDDING_DIM 常量没人改。结果:

    documents 列停在 vector(1024),查询向量是 768
    → 每次语义检索 asyncpg 报 "different vector dimensions"
    → 知识检索静默降级成纯 BM25,**整整 5 天无人察觉**

维度已收敛到单一配置源(core_settings.embedding_dim),但只要模型还能换、迁移
还要手工做,列与配置就仍可能走岔。所以加这道自检:启动时比对**每个** pgvector
列的实际维度与配置维度,不一致就大声报错并挂到 /health,让既有的健康探针
(hcc_health_probe.py 会邮件告警)当场抓到。

**刻意不拒绝启动**:HCC 不能挂——宁可响铃也不停机。维度不符只会让相关检索
降级,不该连带把整个记忆系统一起带走。
"""
from __future__ import annotations

import logging
import re

from sqlalchemy import text

logger = logging.getLogger(__name__)

__all__ = ["check_vector_dims", "get_last_report"]

# 最近一次自检结果,供 /health 暴露(启动时写入)。
_LAST_REPORT: dict = {"checked": False, "ok": True, "mismatches": []}

_VECTOR_TYPE_RE = re.compile(r"vector\((\d+)\)")


def get_last_report() -> dict:
    """返回最近一次自检结果(供 /health 挂载)。"""
    return dict(_LAST_REPORT)


async def check_vector_dims(conn, expected_dim: int) -> dict:
    """比对库里所有 pgvector 列的维度与 ``expected_dim``。

    返回 ``{"checked", "ok", "expected_dim", "columns", "mismatches"}``。
    只读:发现不一致只报警,不自动改表——改表意味着丢弃既有向量,那是需要人
    决断的事(要重嵌入哪张表、什么时候跑),不该在启动路径上偷偷做。
    """
    global _LAST_REPORT
    rows = await conn.execute(text("""
        SELECT c.relname AS table_name,
               a.attname AS column_name,
               format_type(a.atttypid, a.atttypmod) AS col_type
        FROM pg_attribute a
        JOIN pg_class c ON c.oid = a.attrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind = 'r'
          AND NOT a.attisdropped
          AND n.nspname = current_schema()
          AND format_type(a.atttypid, a.atttypmod) LIKE 'vector(%'
        ORDER BY c.relname, a.attname
    """))

    columns: list[dict] = []
    mismatches: list[dict] = []
    for table_name, column_name, col_type in rows:
        m = _VECTOR_TYPE_RE.search(col_type or "")
        actual = int(m.group(1)) if m else None
        entry = {"table": table_name, "column": column_name, "dim": actual}
        columns.append(entry)
        if actual is not None and actual != expected_dim:
            mismatches.append(entry)

    report = {
        "checked": True,
        "ok": not mismatches,
        "expected_dim": expected_dim,
        "columns": columns,
        "mismatches": mismatches,
    }
    _LAST_REPORT = report

    if mismatches:
        detail = ", ".join(f"{m['table']}.{m['column']}=vector({m['dim']})" for m in mismatches)
        logger.error(
            "向量维度不一致!配置 embedding_dim=%d,但这些列不是:%s。"
            "这些表的语义检索会报 'different vector dimensions' 并静默降级为 BM25。"
            "处置:确认目标维度后,清空并重嵌入这些表(参见 2026-08-29 f75ab97 漏迁移事故)。",
            expected_dim, detail,
        )
    else:
        logger.info("向量维度自检通过:%d 个向量列全部 = %d 维", len(columns), expected_dim)
    return report

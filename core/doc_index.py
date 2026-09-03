"""文档索引的共享定义与增量同步 —— 让 md 改动能被自动检测并重新索引。

为什么有这个模块(2026-09-03):

    知识检索原先直接读文件(每查一次全量 read+parse,683ms),慢但有一个优点:
    **永远新鲜**。改用 `documents` 表的 BM25+向量检索后速度和质量都上来了,但若
    不加变更检测,就会静默服务过期内容——正是今天修了一整天的那类失效方式。

    所以索引不是一次性的:本模块按 **(路径, mtime_ns, 大小)** 做签名,只有真正
    变过的文件才去 read+hash+嵌入。没变时一次巡检只有 stat 开销(实测 ~4ms/1124
    文件),因此可以跑得很勤,新鲜度接近实时。

``EXCLUDE_DIRS`` / ``iter_indexable_files`` 原本定义在 scripts/index_documents.py,
现移到这里作为**唯一来源**,脚本改为从此处导入——同一份规则出现在两个地方,迟早
会漂(今天的向量事故就是同一个知识有两个定义造成的)。
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

_MISSING = object()

# 单轮处理上限:防止任何一轮变成长时间占用(2026-09-03 首版一轮处理 1053 个
# 文件,把网关事件循环拖到拒连)。
_MAX_PER_PASS = 150

logger = logging.getLogger("hcc.doc_index")

__all__ = [
    "file_mtime_utc", "EXCLUDE_DIRS", "DEFAULT_COLLECTIONS", "iter_indexable_files",
    "DocIndexSync", "knowledge_path_prefix",
]

EXCLUDE_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", ".obsidian", "dist-info",
    # Archive / backup material: physically or logically excluded from the
    # index on purpose (see docs on Obsidian export — Archive is meant to be
    # invisible to the indexer, not just low-priority).
    "Archive", "归档", "完整备份", "AICore-Archive",
    # core/agent_export.py dumps every active Memory (all types, incl. noisy
    # tool_result/exec logs) as one file per row — thousands of tiny files.
    # That content is already searchable natively via /memory/search and
    # /memory/hybrid-search; re-indexing it into `documents` would duplicate
    # it and drown out the curated collections with low-value noise. The
    # agents/ tree is meant for browsing (see gateway/api/vault_routes.py),
    # not document search.
    "agents",
}

DEFAULT_COLLECTIONS = {
    # Full AICore vault scan — the previous default only covered the
    # 含烟记忆系统 subtree, silently skipping Tasks/, reports/, 日常流程/.
    "aicore": "~/workspace/AICore",
}

# 索引 PDF 与否(HCC_INDEX_PDF=0 只索引 markdown)。
INDEX_PDF = os.environ.get("HCC_INDEX_PDF", "1").strip().lower() not in ("0", "false", "no", "")


def file_mtime_utc(path: Path) -> datetime:
    """文件 mtime,统一为 **naive-UTC** —— 与库中存储、与索引脚本共用同一个定义。

    2026-09-03 实测教训:这里一度用本地时间 ``datetime.fromtimestamp()``,而
    scripts/index_documents.py 存的是 UTC,两者差 10 小时 → 首轮对账把全部 2054
    个文件都误判成"磁盘比库新",触发全量重嵌入。同一个量有两处算法,就一定会
    出这种事(今天的向量维度事故同源)。所以它只许有这一个定义。
    """
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).replace(tzinfo=None)


def iter_indexable_files(root: Path) -> Iterator[Path]:
    """遍历 ``root`` 下可索引的文件(跳过 EXCLUDE_DIRS)。"""
    suffixes = (".md", ".pdf") if INDEX_PDF else (".md",)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
        for name in filenames:
            if name.lower().endswith(suffixes):
                yield Path(dirpath) / name


def knowledge_path_prefix() -> str:
    """知识子树在 ``documents.path`` 里的前缀(相对 aicore 根)。

    知识检索按此前缀过滤,而不是为 Knowledge 单开一个 collection —— 单开会让
    aicore 全量扫描与它重复索引同一批文件(1129 篇的内容与向量各存两份)。
    """
    from core.config import core_settings
    qmd = Path(core_settings.qmd_dir).expanduser()
    root = Path(DEFAULT_COLLECTIONS["aicore"]).expanduser()
    try:
        return f"{qmd.relative_to(root)}/Knowledge/"
    except ValueError:  # qmd_dir 不在 aicore 根下 → 不加前缀限制
        logger.warning("qmd_dir %s 不在 aicore 根 %s 之下,知识检索不做子树过滤", qmd, root)
        return ""


class DocIndexSync:
    """按 stat 签名做增量索引:只有 mtime/大小变过的文件才重新读取与嵌入。"""

    def __init__(self) -> None:
        # abs_path -> (mtime_ns, size);进程内常驻,首轮建立
        self._sig: dict[str, tuple[int, int]] = {}
        self._primed = False

    @staticmethod
    def _scan(root: Path) -> dict[str, tuple[int, int]]:
        """遍历 + stat(阻塞,由 to_thread 调用)。"""
        out: dict[str, tuple[int, int]] = {}
        for path in iter_indexable_files(root):
            try:
                st = path.stat()
            except OSError:
                continue
            out[str(path)] = (st.st_mtime_ns, st.st_size)
        return out

    @staticmethod
    def _read_and_hash(path: Path) -> tuple[bytes, str]:
        """读文件 + 算哈希(阻塞,由 to_thread 调用)。"""
        raw = path.read_bytes()
        return raw, hashlib.sha256(raw).hexdigest()

    async def sync_once(self, *, embed: bool = True) -> dict:
        """巡检一轮。返回 ``{scanned, changed, deleted, errors}``。"""
        from sqlalchemy import select, update

        from gateway.core.database import async_session
        from gateway.models import Document
        from gateway.core.embeddings import document_embedding_text, embed_text
        from gateway.services.document_service import DocumentService

        stats = {"scanned": 0, "changed": 0, "touched": 0, "deleted": 0, "errors": 0}
        for collection, root_str in DEFAULT_COLLECTIONS.items():
            root = Path(root_str).expanduser()
            if not root.exists():
                continue

            # **所有阻塞工作都必须离开事件循环**。2026-09-03 教训:首版把 os.walk /
            # read_bytes / sha256 / embed_text(sentence-transformers 推理)直接写在
            # 协程里,首轮处理 1053 个文件时把事件循环整整堵死——网关既慢到 2.2s
            # 又直接拒连,是我亲手造成的一次宕机。库里其他路径早就用
            # asyncio.to_thread 包住 embed_text,这里漏了。
            seen = await asyncio.to_thread(self._scan, root)
            stats["scanned"] += len(seen)
            # 只有 mtime 变、**大小没变**的,直接刷新签名不读文件:QMD 生成器每轮把
            # 1129 篇 Knowledge 原样重写一遍(内容相同,仅 mtime 变),若还去读+哈希,
            # 就是每 60 秒白读 125MB —— 实测正是它把网关拖垮的。代价:同尺寸的内容
            # 改动会被漏掉,由下面的低频全量校验兜底。
            changed_paths = []
            for k, v in seen.items():
                prev = self._sig.get(k)
                if prev == v:
                    continue
                if prev is not None and prev[1] == v[1]:
                    continue  # 大小未变 → 视为生成器空写
                changed_paths.append(Path(k))

            # 首轮:不能只建签名基线就了事 —— 网关停机期间新增/改动的文件会被
            # 悄悄吞掉(2026-09-03 实测:探针文件正是这样被漏掉的)。改为与库里
            # 存的 mtime 比对,只把"库里没有"或"磁盘比库新"的当作变更。
            if not self._primed:
                async with async_session() as session:
                    rows = await session.execute(
                        select(Document.path, Document.mtime).where(Document.collection == collection))
                    db_mtime = {p_: m for p_, m in rows}
                changed_paths = []
                for path in map(Path, seen):
                    rel = str(path.relative_to(root))
                    known = db_mtime.get(rel, _MISSING)
                    if known is _MISSING:
                        changed_paths.append(path)          # 库里没有 → 新文件
                    elif known is not None:
                        disk = file_mtime_utc(path)
                        if disk > known:
                            changed_paths.append(path)      # 磁盘比库新 → 停机期间改过
                    # known is None(旧数据没存 mtime)→ 视为已同步,不做全量重算
                self._primed = True
                logger.info("doc index sync: 首轮对账 %d 文件,需补索引 %d",
                            len(seen), len(changed_paths))

            removed = [p for p in self._sig if p not in seen]
            # 磁盘上真实存在的全部相对路径 —— **必须在下面的"推迟"pop 之前定下**,
            # 否则被推迟到下轮的文件会因不在 keep 里而被 delete_missing 误删。
            keep_rels = {str(Path(k).relative_to(root)) for k in seen}
            self._sig = seen
            if not changed_paths and not removed:
                return stats

            # 取候选文件在库里的 content_hash:mtime 只是**廉价预筛**,真正决定要不要
            # 重算的是内容哈希。QMD 生成器会周期性重写整棵 Knowledge 树(实测 1129 篇
            # 的 mtime 每轮都变),若只看 mtime 就会每轮把它们全部重新嵌入一遍——
            # 纯粹空转。内容没变就只刷新 mtime,不读不算。
            # 每轮处理量封顶:任何一轮都不许变重(首轮补账、或一次性大批改动时,
            # 分摊到多轮完成,下一轮继续 —— 签名只在处理成功后才更新)。
            if len(changed_paths) > _MAX_PER_PASS:
                logger.info("doc index sync: 本轮只处理 %d/%d,其余下轮继续",
                            _MAX_PER_PASS, len(changed_paths))
                deferred = changed_paths[_MAX_PER_PASS:]
                changed_paths = changed_paths[:_MAX_PER_PASS]
                for p_ in deferred:      # 让它们下轮仍被视为待处理
                    seen.pop(str(p_), None)
            rels = [str(p.relative_to(root)) for p in changed_paths]
            async with async_session() as session:
                known_hash = {}
                if rels:
                    rows = await session.execute(
                        select(Document.path, Document.content_hash)
                        .where(Document.collection == collection, Document.path.in_(rels)))
                    known_hash = {p_: h for p_, h in rows}

                svc = DocumentService(session)
                for path in changed_paths:
                    try:
                        rel = str(path.relative_to(root))
                        raw, digest = await asyncio.to_thread(self._read_and_hash, path)
                        if known_hash.get(rel) == digest:
                            # 内容没变(生成器只是重写了文件)→ 只刷新 mtime,免掉嵌入
                            await session.execute(
                                update(Document)
                                .where(Document.collection == collection, Document.path == rel)
                                .values(mtime=file_mtime_utc(path)))
                            stats["touched"] = stats.get("touched", 0) + 1
                            continue
                        content = raw.decode("utf-8", errors="ignore")
                        title = path.stem
                        emb = None
                        if embed:
                            try:
                                emb = await asyncio.to_thread(
                                    embed_text, document_embedding_text(title, content))
                            except Exception:  # 嵌入失败不该挡住 BM25 索引
                                logger.warning("doc index: 嵌入失败 %s", rel, exc_info=True)
                        await svc.upsert(
                            collection=collection, path=rel, title=title, content=content,
                            content_hash=digest, embedding=emb, mtime=file_mtime_utc(path),
                        )
                        stats["changed"] += 1
                    except Exception:
                        stats["errors"] += 1
                        logger.warning("doc index: 处理失败 %s", path, exc_info=True)
                if removed:
                    stats["deleted"] += await svc.delete_missing(collection, keep_rels)
                await session.commit()

        if stats["changed"] or stats["deleted"] or stats["touched"]:
            logger.info(
                "doc index sync: 重算 %(changed)d, 内容未变仅刷新 %(touched)d, 删除 %(deleted)d, 失败 %(errors)d",
                stats)
        return stats

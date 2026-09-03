"""QMD (markdown) knowledge :class:`~core.providers.base.Provider`.

Wraps the file-based knowledge base produced by
:class:`core.qmd_generator.QMDGenerator` behind the Provider SDK. Where the
generator projects PostgreSQL memories *out* to markdown, this provider offers
read/write access *over* those same markdown documents so the knowledge layer
can be searched and edited through the uniform Provider interface.

Documents live under ``<HCC_QMD_DIR>/Knowledge/<Category>/<slug>.md`` and carry
YAML front matter plus a top-level ``# heading``.

Configuration
-------------
``HCC_KNOWLEDGE_PROVIDER``
    Backend selector; only ``qmd`` (the default) is supported today.
``HCC_QMD_DIR``
    Root directory containing the ``Knowledge`` tree (shared with the QMD
    generator via :class:`core.config.CoreSettings`).
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from core.config import CoreSettings, core_settings
from gateway.core.fts import tokenize_for_fts
from core.providers.base import (
    HealthStatus,
    Provider,
    ProviderMetadata,
    SearchQuery,
    SearchResult,
    StoreData,
    StoreResult,
    UpdateData,
)

logger = logging.getLogger(__name__)

# 解析结果缓存:path -> (mtime_ns, size, item, haystack)。见 _parse_cached 说明。
# haystack = 小写化的 "heading\ncontent",**预先算好**:匹配循环原本每次查询都要把
# 1124 篇全文重新 .lower() 拷贝一遍(profile 实测 288ms,占知识检索 93%)。
# 进程内常驻(网关常开),语料纯文本,内存开销可忽略。
_PARSE_CACHE: dict[str, tuple[int, int, dict, str]] = {}

__all__ = ["KnowledgeProviderSettings", "KnowledgeQMDProvider"]

# type/tag keyword -> knowledge category (sub-folder). Mirrors the generator.
_CATEGORY_RULES: dict[str, str] = {
    "person": "People",
    "people": "People",
    "contact": "People",
    "profile": "People",
    "project": "Projects",
    "product": "Projects",
    "rule": "Rules",
    "principle": "Rules",
    "policy": "Rules",
    "preference": "Rules",
    "event": "Timeline",
    "timeline": "Timeline",
    "milestone": "Timeline",
}
DEFAULT_CATEGORY = "General"

_HEADING_RE = re.compile(r"^#\s+(.*)$", re.MULTILINE)
_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)


class KnowledgeProviderSettings(BaseSettings):
    """Settings for the QMD knowledge provider (``HCC_*`` env vars)."""

    model_config = SettingsConfigDict(
        env_prefix="HCC_", env_file=".env", extra="ignore"
    )

    knowledge_provider: str = Field(
        default="qmd",
        description="Knowledge backend selector (HCC_KNOWLEDGE_PROVIDER).",
    )


def _slugify(value: str, *, max_len: int = 60) -> str:
    """Return a filesystem-safe, lowercase slug derived from ``value``."""
    value = (value or "").strip().lower()
    slug = re.sub(r"[^\w]+", "-", value, flags=re.UNICODE).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)
    if len(slug) > max_len:
        slug = slug[:max_len].rstrip("-")
    return slug or "untitled"


class KnowledgeQMDProvider(Provider):
    """Provider offering search/CRUD over the QMD markdown knowledge base.

    Parameters
    ----------
    settings:
        Optional :class:`KnowledgeProviderSettings` (backend selector).
    core_settings_:
        Optional :class:`CoreSettings` supplying ``qmd_dir``. Injectable for
        tests; defaults to the shared module singleton.
    """

    name = "knowledge_qmd"
    version = "0.1.0"

    def __init__(
        self,
        *,
        settings: KnowledgeProviderSettings | None = None,
        core_settings_: CoreSettings | None = None,
    ) -> None:
        self._settings = settings or KnowledgeProviderSettings()
        self._core = core_settings_ or core_settings

    @property
    def root(self) -> Path:
        """Return the ``Knowledge`` root directory under ``HCC_QMD_DIR``."""
        return Path(self._core.qmd_dir).expanduser() / "Knowledge"

    # ------------------------------------------------------------------
    # Rendering / parsing helpers
    # ------------------------------------------------------------------
    def _category_for(self, type_: str, tags: list[str]) -> str:
        """Map a document ``type``/``tags`` onto a knowledge category folder."""
        key = (type_ or "").strip().lower()
        if key in _CATEGORY_RULES:
            return _CATEGORY_RULES[key]
        for tag in tags or []:
            tkey = str(tag).strip().lower()
            if tkey in _CATEGORY_RULES:
                return _CATEGORY_RULES[tkey]
        return DEFAULT_CATEGORY

    @staticmethod
    def _render(data: StoreData, *, title: str) -> str:
        """Render ``data`` to a markdown document with YAML front matter."""
        frontmatter = {
            "type": data.type,
            "user_id": data.user_id,
            "importance": data.importance,
            "tags": list(data.tags or []),
            "source": data.source,
        }
        fm_yaml = yaml.safe_dump(
            frontmatter, sort_keys=False, allow_unicode=True
        ).strip()
        parts = [f"---\n{fm_yaml}\n---", f"# {title}", ""]
        if data.summary and data.summary.strip() != title:
            parts.append(f"> {data.summary.strip()}\n")
        parts.append((data.content or "").strip())
        if data.tags:
            tag_line = " ".join(f"#{_slugify(str(t))}" for t in data.tags)
            parts.append(f"\n---\n\nTags: {tag_line}")
        return "\n".join(parts).rstrip() + "\n"

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        """Write ``content`` to ``path`` atomically (temp file + rename)."""
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(content)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    def _parse_cached(self, path: Path) -> dict[str, Any]:
        """带缓存的 :meth:`_parse` —— 键是 (路径, mtime_ns, 大小)。

        2026-09-03 profile:``/context`` p50=783ms,其中 **683ms(88%)** 花在知识
        检索——因为每次查询都把 Knowledge 下的 1129 个 md 全量 read_text + YAML
        前言解析一遍。语料几乎不变,却每轮对话重做一次全量 I/O。

        用 stat 做键而不是 TTL:文件一改 mtime/大小就变,缓存自动失效,**没有任何
        陈旧窗口**;目录仍每次 rglob,新增文件立刻可见。正确性与改前完全一致。
        """
        try:
            st = path.stat()
            key = (str(path), st.st_mtime_ns, st.st_size)
        except OSError:
            return self._parse(path)
        hit = _PARSE_CACHE.get(key[0])
        if hit is not None and hit[0] == key[1] and hit[1] == key[2]:
            return hit[2]
        item = self._parse(path)
        hay = f"{item['heading']}\n{item['content']}".lower()
        _PARSE_CACHE[key[0]] = (key[1], key[2], item, hay)
        return item

    def _cached_haystack(self, path: Path, item: dict[str, Any]) -> str:
        """取预先小写化的检索文本(缓存未命中则现算,保证正确性)。"""
        hit = _PARSE_CACHE.get(str(path))
        if hit is not None and hit[2] is item:
            return hit[3]
        return f"{item['heading']}\n{item['content']}".lower()

    def _parse(self, path: Path) -> dict[str, Any]:
        """Parse a markdown document into a JSON-safe item dict."""
        raw = path.read_text(encoding="utf-8")
        front: dict[str, Any] = {}
        body = raw
        fm_match = _FRONTMATTER_RE.match(raw)
        if fm_match:
            try:
                front = yaml.safe_load(fm_match.group(1)) or {}
            except yaml.YAMLError:
                front = {}
            body = raw[fm_match.end():]
        heading_match = _HEADING_RE.search(body)
        heading = heading_match.group(1).strip() if heading_match else path.stem
        rel = path.relative_to(self.root)
        return {
            "id": str(rel),
            "path": str(path),
            "category": rel.parts[0] if len(rel.parts) > 1 else DEFAULT_CATEGORY,
            "heading": heading,
            "content": body.strip(),
            "type": front.get("type"),
            "user_id": front.get("user_id"),
            "importance": front.get("importance"),
            "tags": list(front.get("tags") or []),
            "source": front.get("source"),
        }

    def _iter_docs(self) -> list[Path]:
        """Return all knowledge markdown docs (excluding README indexes)."""
        if not self.root.exists():
            return []
        return sorted(
            p
            for p in self.root.rglob("*.md")
            if p.name.lower() != "readme.md"
        )

    def _resolve(self, id: str) -> Path:
        """Resolve a document ``id`` (relative path) to a path under the root.

        Guards against path-traversal: any ``id`` that escapes the knowledge
        root resolves back to the root itself (so callers get a non-existent
        path rather than reading arbitrary files).
        """
        root = self.root
        candidate = (root / id)
        try:
            candidate.resolve().relative_to(root.resolve())
        except ValueError:
            # Escaped the root -> return a sentinel path that never exists so
            # callers treat it as "not found" rather than touching real files.
            return root / "__invalid__"
        # Return the un-resolved path so it stays comparable with ``self.root``
        # in downstream ``relative_to`` calls (avoids symlink normalisation).
        return candidate

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------
    async def search(self, query: SearchQuery) -> SearchResult:
        """Search knowledge documents by heading/content (and optional type).

        The query is jieba-segmented (same tokenizer used for memory BM25,
        see :mod:`gateway.core.fts`) and a document matches if *any* token
        appears in its heading/body. A whole-sentence ``ilike`` substring
        test (the previous behaviour) virtually never matches once the query
        is a full sentence rather than a single word/phrase copied verbatim
        from a document — segmenting first is what lets long free-text
        queries still surface relevant notes. Results are ranked by token
        hit count (most matching tokens first), then paginated with
        ``offset``/``limit``. An empty ``query`` lists everything.
        """
        tokens = tokenize_for_fts(query.query or "").split()
        # token 小写只做一次 —— 原本写在 per-doc 循环里,1124 篇 × N token 次冗余调用
        tokens_lower = [t.lower() for t in tokens]
        matches: list[tuple[int, dict[str, Any]]] = []
        for path in self._iter_docs():
            try:
                item = self._parse_cached(path)
            except OSError:
                logger.warning("Failed to read QMD doc %s", path, exc_info=True)
                continue
            if query.type and (item.get("type") or "").lower() != query.type.lower():
                continue
            if query.user_id and str(item.get("user_id")) != str(query.user_id):
                continue
            if tokens:
                haystack = self._cached_haystack(path, item)
                hits = sum(1 for tok in tokens_lower if tok in haystack)
                if not hits:
                    continue
                matches.append((hits, item))
            else:
                matches.append((0, item))

        matches.sort(key=lambda pair: pair[0], reverse=True)
        ordered = [item for _, item in matches]
        total = len(ordered)
        page = ordered[query.offset : query.offset + query.limit]
        return SearchResult(items=page, total=total, provider=self.name)

    # ------------------------------------------------------------------
    # Store / update
    # ------------------------------------------------------------------
    async def store(self, data: StoreData) -> StoreResult:
        """Create (or overwrite) a QMD markdown document for ``data``."""
        title = (data.summary or data.content or "untitled").splitlines()[0][:100]
        category = self._category_for(data.type, list(data.tags or []))
        slug = _slugify(title)
        path = self.root / category / f"{slug}.md"
        try:
            self._atomic_write(path, self._render(data, title=title))
        except OSError:
            logger.exception("KnowledgeQMDProvider.store failed for %s", path)
            return StoreResult(id="", success=False, provider=self.name)
        rel = path.relative_to(self.root)
        return StoreResult(id=str(rel), success=True, provider=self.name)

    async def update(self, data: UpdateData) -> StoreResult:
        """Patch an existing markdown document identified by ``data.id``.

        ``data.id`` is the document's path relative to the ``Knowledge`` root.
        Only supplied fields are changed; the file is re-rendered in place.
        """
        path = self._resolve(data.id)
        if not path.exists():
            return StoreResult(id=data.id, success=False, provider=self.name)
        current = self._parse(path)
        merged = StoreData(
            content=data.content if data.content is not None else current["content"],
            user_id=str(current.get("user_id") or ""),
            type=str(current.get("type") or "general"),
            summary=data.summary if data.summary is not None else current["heading"],
            importance=(
                data.importance
                if data.importance is not None
                else float(current.get("importance") or 0.5)
            ),
            tags=data.tags if data.tags is not None else list(current.get("tags") or []),
            source=str(current.get("source") or "api"),
        )
        title = (merged.summary or merged.content or "untitled").splitlines()[0][:100]
        try:
            self._atomic_write(path, self._render(merged, title=title))
        except OSError:
            logger.exception("KnowledgeQMDProvider.update failed for %s", path)
            return StoreResult(id=data.id, success=False, provider=self.name)
        return StoreResult(id=data.id, success=True, provider=self.name)

    async def delete(self, id: str) -> bool:
        """Delete the markdown document identified by ``id`` (relative path)."""
        path = self._resolve(id)
        if not path.exists():
            return False
        try:
            path.unlink()
        except OSError:
            logger.exception("KnowledgeQMDProvider.delete failed for %s", path)
            return False
        return True

    # ------------------------------------------------------------------
    # Health & metadata
    # ------------------------------------------------------------------
    async def health(self) -> HealthStatus:
        """Report health based on readability of the knowledge root."""
        start = time.perf_counter()
        healthy = True
        try:
            # Root need not exist yet (empty KB); only fail on real IO errors.
            if self.root.exists():
                _ = list(self.root.iterdir())
        except OSError:
            logger.exception("KnowledgeQMDProvider health check failed")
            healthy = False
        latency_ms = (time.perf_counter() - start) * 1000.0
        return HealthStatus(
            healthy=healthy,
            version=self.version,
            provider_name=self.name,
            latency_ms=round(latency_ms, 3),
        )

    async def metadata(self) -> ProviderMetadata:
        """Return static provider metadata (capabilities + config)."""
        return ProviderMetadata(
            name=self.name,
            version=self.version,
            capabilities=["search", "store", "update", "delete"],
            config={
                "backend": self._settings.knowledge_provider,
                "qmd_dir": str(self._core.qmd_dir),
            },
        )

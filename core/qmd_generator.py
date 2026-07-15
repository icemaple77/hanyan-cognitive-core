"""Knowledge Document (QMD) generator for HCC v2.

Reads durable :class:`~gateway.models.Memory` rows from PostgreSQL and renders
them as human-readable, git-friendly markdown files organised by category.
Files are written atomically (temp file + rename) and per-category ``README``
index files are (re)generated on every run. An optional git commit captures a
snapshot of the knowledge base.

Output layout (relative to ``HCC_QMD_DIR``)::

    Knowledge/README.md              <- master index across all categories
    Knowledge/People/README.md       <- per-category index
    Knowledge/People/<slug>.md
    Knowledge/Projects/README.md
    Knowledge/Projects/<slug>.md
    Knowledge/Timeline/...
    Knowledge/Rules/...
    Knowledge/General/...
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from core.config import CoreSettings, core_settings
from gateway.core.database import async_session
from gateway.models import Memory

logger = logging.getLogger(__name__)

# Mapping of Memory.type / tag keywords to a knowledge category (sub-folder).
# Unmatched memories land in the ``General`` bucket.
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

# Categories for which an index README is always generated, even when empty.
_ALWAYS_INDEX = ("People", "Projects")


@dataclass
class GenerationStats:
    """Summary of a generation run."""

    total_memories: int = 0
    files_written: int = 0
    indexes_written: int = 0
    categories: dict[str, int] = field(default_factory=dict)
    git_committed: bool = False

    def as_dict(self) -> dict:
        """Return the stats as a plain dict (for logging / JSON)."""
        return {
            "total_memories": self.total_memories,
            "files_written": self.files_written,
            "indexes_written": self.indexes_written,
            "categories": self.categories,
            "git_committed": self.git_committed,
        }


def _slugify(text: str, *, max_len: int = 60) -> str:
    """Return a filesystem-safe, lowercase slug derived from ``text``."""
    text = (text or "").strip().lower()
    # Replace any run of non-alphanumerics (incl. unicode spaces) with a dash.
    slug = re.sub(r"[^\w]+", "-", text, flags=re.UNICODE).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)
    if len(slug) > max_len:
        slug = slug[:max_len].rstrip("-")
    return slug or "untitled"


class QMDGenerator:
    """Generate markdown knowledge documents from PostgreSQL memories.

    Parameters
    ----------
    settings:
        Optional :class:`CoreSettings`. Defaults to the module singleton.
    session_factory:
        Optional async sessionmaker; defaults to the gateway's ``async_session``.
        Injectable for testing.
    """

    def __init__(
        self,
        *,
        settings: CoreSettings | None = None,
        session_factory: async_sessionmaker | None = None,
    ) -> None:
        self._settings = settings or core_settings
        self._session_factory = session_factory or async_session

    @property
    def root(self) -> Path:
        """Return the ``Knowledge`` root directory under ``HCC_QMD_DIR``."""
        return Path(self._settings.qmd_dir).expanduser() / "Knowledge"

    # ------------------------------------------------------------------
    # Categorisation & rendering helpers
    # ------------------------------------------------------------------
    def _categorize(self, memory: Memory) -> str:
        """Return the knowledge category (sub-folder) for ``memory``."""
        mtype = (memory.type or "").strip().lower()
        if mtype in _CATEGORY_RULES:
            return _CATEGORY_RULES[mtype]
        for tag in memory.tags or []:
            key = str(tag).strip().lower()
            if key in _CATEGORY_RULES:
                return _CATEGORY_RULES[key]
        return DEFAULT_CATEGORY

    @staticmethod
    def _title(memory: Memory) -> str:
        """Derive a human-readable title from a memory's summary/content."""
        summary = (memory.summary or "").strip()
        if summary:
            return summary.splitlines()[0][:100]
        content = (memory.content or "").strip()
        if content:
            return content.splitlines()[0][:100]
        return f"Memory {memory.id[:8]}"

    def _render_memory(self, memory: Memory) -> str:
        """Render a single memory to a markdown document with YAML front matter."""
        frontmatter = {
            "id": memory.id,
            "type": memory.type,
            "user_id": memory.user_id,
            "importance": memory.importance,
            "tags": list(memory.tags or []),
            "source": memory.source,
            "status": memory.status,
            "created_at": self._fmt_dt(memory.created_at),
            "updated_at": self._fmt_dt(memory.updated_at),
        }
        fm_yaml = yaml.safe_dump(
            frontmatter, sort_keys=False, allow_unicode=True
        ).strip()

        title = self._title(memory)
        parts = [f"---\n{fm_yaml}\n---", f"# {title}", ""]
        if memory.summary and memory.summary.strip() != title:
            parts.append(f"> {memory.summary.strip()}\n")
        parts.append((memory.content or "").strip())
        if memory.tags:
            tag_line = " ".join(f"#{_slugify(str(t))}" for t in memory.tags)
            parts.append(f"\n---\n\nTags: {tag_line}")
        return "\n".join(parts).rstrip() + "\n"

    @staticmethod
    def _fmt_dt(value: datetime | None) -> str:
        """Format a (possibly naive) datetime as an ISO-8601 string."""
        if value is None:
            return ""
        return value.isoformat()

    def _render_index(self, category: str, entries: list[tuple[str, Memory]]) -> str:
        """Render a per-category README linking to each document."""
        lines = [
            f"# {category}",
            "",
            f"_{len(entries)} document(s). Auto-generated by HCC QMDGenerator._",
            "",
        ]
        for slug, memory in sorted(entries, key=lambda e: e[0]):
            title = self._title(memory)
            summary = (memory.summary or "").strip().replace("\n", " ")
            suffix = f" — {summary[:120]}" if summary and summary != title else ""
            lines.append(f"- [{title}]({slug}.md){suffix}")
        lines.append("")
        return "\n".join(lines)

    def _render_master_index(
        self, grouped: dict[str, list[tuple[str, Memory]]]
    ) -> str:
        """Render the top-level ``Knowledge/README.md`` index."""
        total = sum(len(v) for v in grouped.values())
        stamp = datetime.now(timezone.utc).isoformat()
        lines = [
            "# Knowledge Base",
            "",
            f"_Auto-generated by HCC QMDGenerator on {stamp}._",
            "",
            f"Total documents: **{total}**",
            "",
            "## Categories",
            "",
        ]
        for category in sorted(grouped):
            count = len(grouped[category])
            lines.append(f"- [{category}]({category}/README.md) ({count})")
        lines.append("")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Atomic file writing
    # ------------------------------------------------------------------
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
            # Clean up the temp file on any failure.
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    # ------------------------------------------------------------------
    # Data access
    # ------------------------------------------------------------------
    async def _fetch_memories(self) -> list[Memory]:
        """Load all active memories from PostgreSQL, oldest first."""
        async with self._session_factory() as session:
            result = await session.execute(
                select(Memory)
                .where(Memory.status == "active")
                .where(Memory.shared == True)
                .order_by(Memory.created_at)
            )
            return list(result.scalars().all())

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    async def generate_all(self) -> GenerationStats:
        """Generate the full knowledge base and return :class:`GenerationStats`."""
        memories = await self._fetch_memories()
        stats = GenerationStats(total_memories=len(memories))

        # Group memories into categories, ensuring per-category slug uniqueness.
        grouped: dict[str, list[tuple[str, Memory]]] = {}
        seen_slugs: dict[str, set[str]] = {}
        for memory in memories:
            category = self._categorize(memory)
            base_slug = _slugify(self._title(memory))
            used = seen_slugs.setdefault(category, set())
            slug = base_slug
            n = 1
            while slug in used:
                n += 1
                slug = f"{base_slug}-{n}"
            used.add(slug)
            grouped.setdefault(category, []).append((slug, memory))

        # Ensure the always-present categories exist as (possibly empty) buckets.
        for category in _ALWAYS_INDEX:
            grouped.setdefault(category, [])

        # Write documents.
        for category, entries in grouped.items():
            for slug, memory in entries:
                path = self.root / category / f"{slug}.md"
                self._atomic_write(path, self._render_memory(memory))
                stats.files_written += 1
            stats.categories[category] = len(entries)

            # Per-category index.
            index_path = self.root / category / "README.md"
            self._atomic_write(index_path, self._render_index(category, entries))
            stats.indexes_written += 1

        # Master index.
        self._atomic_write(
            self.root / "README.md", self._render_master_index(grouped)
        )
        stats.indexes_written += 1

        logger.info("QMD generation complete: %s", stats.as_dict())

        if self._settings.qmd_git_enabled:
            stats.git_committed = self._git_commit(stats)

        return stats

    # ------------------------------------------------------------------
    # Git integration (optional)
    # ------------------------------------------------------------------
    def _git_commit(self, stats: GenerationStats) -> bool:
        """Stage and commit the QMD directory. Returns ``True`` on commit."""
        repo = Path(self._settings.qmd_dir).expanduser()
        try:
            if not (repo / ".git").exists():
                self._run_git(["init"], repo)
            self._run_git(["add", "-A"], repo)
            # Nothing to commit -> `git diff --cached --quiet` exits 0.
            diff = subprocess.run(
                ["git", "diff", "--cached", "--quiet"],
                cwd=str(repo),
            )
            if diff.returncode == 0:
                logger.info("QMD git: no changes to commit")
                return False
            msg = (
                f"chore(qmd): regenerate knowledge base "
                f"({stats.files_written} docs)"
            )
            self._run_git(["commit", "-m", msg], repo)
            logger.info("QMD git: committed knowledge base")
            return True
        except (subprocess.CalledProcessError, OSError):
            logger.exception("QMD git commit failed")
            return False

    @staticmethod
    def _run_git(args: Iterable[str], cwd: Path) -> None:
        """Run a git command in ``cwd``, raising on failure."""
        subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            check=True,
            capture_output=True,
            text=True,
        )


async def _amain() -> None:
    """Async entrypoint: run a full generation pass and print stats."""
    generator = QMDGenerator()
    stats = await generator.generate_all()
    print("QMD generation complete:")
    for key, value in stats.as_dict().items():
        print(f"  {key}: {value}")


def main() -> None:
    """CLI entrypoint (``python -m core.qmd_generator``)."""
    import asyncio

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    asyncio.run(_amain())


if __name__ == "__main__":
    main()

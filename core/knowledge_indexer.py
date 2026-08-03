"""Workspace Knowledge Indexer — indexes non-memory workspace files into HCC.

TARGETS (read-only, files never deleted):
- projects/    → Project knowledge entries
- tasks/       → Active tasks & TODOs
- rules/       → Behavior rules & guidelines
- soul/        → Sub-personalities & identity
- skills/      → Agent skill definitions
- network/     → Network & service configurations

Each file is indexed into HCC's Knowledge store (not Memory), with metadata
about its source directory and agent context. Original files are never touched.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# What to index, and what category to assign
WATCH_DIRS: dict[str, str] = {
    "projects": "project",
    "tasks": "task",
    "rules": "rule",
    "soul": "soul",
    "skills": "skill",
    "network": "network",
}

# Files to skip
SKIP_FILES = {"README.md", "DONE.md"}
SKIP_PATTERNS = [r"\.git/", r"node_modules/", r"venv/", r"__pycache__/",
                  r"\.venv/", r"site-packages/", r"dist-info/",
                  r"\.agents/", r"_frontend_code/", r"templates/",
                  r"CHANGELOG", r"LICENSE", r"Privacy"]


class WorkspaceKnowledgeIndexer:
    """Indexes workspace knowledge files into HCC Knowledge store.

    Integrates with:
    - core/providers/knowledge_qmd.py — Knowledge storage
    - core/qmd_generator.py — QMD output
    - gateway/api/context_routes.py — Context API
    - core/managers/knowledge_manager.py — Knowledge orchestration
    """

    def __init__(self, workspace_dir: str | Path | None = None):
        self._workspace_dir = Path(workspace_dir) if workspace_dir else None
        self.indexed_count = 0
        self.last_run: str | None = None

    @property
    def workspace(self) -> Path | None:
        return self._workspace_dir

    @workspace.setter
    def workspace(self, path: str | Path) -> None:
        self._workspace_dir = Path(path)

    def scan(self) -> dict[str, list[dict[str, Any]]]:
        """Scan workspace and categorize all indexable files.

        Returns dict of category → [file info]
        """
        if not self._workspace_dir or not self._workspace_dir.exists():
            return {}

        result: dict[str, list[dict[str, Any]]] = {}
        for dir_name, category in WATCH_DIRS.items():
            target = self._workspace_dir / dir_name
            if not target.exists():
                continue

            files = []
            for f in target.rglob("*.md"):
                rel = f.relative_to(self._workspace_dir)
                rel_str = rel.as_posix()

                # Skip
                if f.name in SKIP_FILES:
                    continue
                if any(re.search(p, rel_str) for p in SKIP_PATTERNS):
                    continue

                files.append(self._parse_file(f, category, rel_str))

            if files:
                result[category] = files
                logger.info("indexer: found %d %s files", len(files), category)

        return result

    def _parse_file(self, path: Path, category: str, rel_path: str) -> dict[str, Any]:
        """Extract metadata and content from a markdown file."""
        content = path.read_text(encoding="utf-8", errors="replace")

        # Extract title from first H1
        title = ""
        summary = ""
        lines = content.split("\n")
        for i, line in enumerate(lines):
            if line.startswith("# ") and not title:
                title = line[2:].strip()
            elif line.startswith("## ") and not summary:
                # Use first H2 or first non-empty paragraph as summary
                pass

        if not title:
            title = path.stem.replace("-", " ").title()

        # First non-empty line after title as summary
        for line in lines:
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and len(stripped) > 10:
                summary = stripped[:200]
                break

        # Extract tags from frontmatter or inline
        tags = [category]
        tag_match = re.findall(r"#(\w[\w-]*)", content)
        if tag_match:
            tags.extend(tag_match[:5])

        # Determine sub-category from directory structure
        parts = rel_path.split("/")
        subcategory = parts[1] if len(parts) > 2 else category

        return {
            "title": title,
            "summary": summary or title,
            "content": content[:10000],  # Cap to reasonable size
            "category": category,
            "subcategory": subcategory,
            "tags": list(set(tags)),
            "source_path": rel_path,
            "file_size": path.stat().st_size,
            "last_modified": datetime.fromtimestamp(
                path.stat().st_mtime, tz=timezone.utc
            ).isoformat(),
        }

    def index_to_knowledge(self, scan_result: dict[str, list[dict[str, Any]]],
                           api_base_url: str = "http://localhost:8000",
                           agent_id: str = "main") -> dict[str, Any]:
        """Send scanned files to HCC Knowledge API."""
        import httpx

        def sanitize(text: str, max_len: int = 8000) -> str:
            # Strip null bytes, normalize unicode, cap length
            cleaned = text.replace("\x00", "").replace("\uffff", "")
            # Replace common problematic control chars
            cleaned = "".join(c if c >= " " or c in "\n\r\t" else " " for c in cleaned)
            # Truncate to safe size for API
            return cleaned[:max_len]

        total = sum(len(files) for files in scan_result.values())
        indexed = 0
        errors = 0
        by_category: dict[str, int] = {}

        for category, files in scan_result.items():
            by_category[category] = 0
            for file_info in files:
                try:
                    resp = httpx.post(
                        f"{api_base_url}/api/v1/memory/store",
                        json={
                            "user_id": agent_id,
                            "agent_id": agent_id,
                            "shared": True,
                            "type": f"knowledge_{category}",
                            "content": sanitize(file_info["content"]),
                            "summary": file_info["summary"],
                            "tags": file_info["tags"],
                            "source": f"workspace/{file_info['source_path']}",
                            "importance": 0.7,
                        },
                        timeout=10,
                    )
                    if resp.status_code == 200:
                        indexed += 1
                        by_category[category] = by_category.get(category, 0) + 1
                    else:
                        errors += 1
                        logger.warning(
                            "indexer: failed to index %s: HTTP %d",
                            file_info["source_path"],
                            resp.status_code,
                        )
                except Exception as e:
                    errors += 1
                    logger.warning("indexer: error indexing %s: %s",
                                   file_info["source_path"], e)

        self.indexed_count = indexed
        self.last_run = datetime.now(timezone.utc).isoformat()

        result = {
            "status": "completed",
            "total_found": total,
            "indexed": indexed,
            "errors": errors,
            "by_category": by_category,
            "last_run": self.last_run,
        }
        logger.info("indexer: %s", result)
        return result

    def get_summary(self) -> dict[str, Any]:
        """Get indexer status summary."""
        scan = self.scan()
        total = sum(len(files) for files in scan.values())
        return {
            "workspace": str(self._workspace_dir) if self._workspace_dir else None,
            "files_found": total,
            "by_category": {k: len(v) for k, v in scan.items()},
            "last_indexed": self.indexed_count,
            "last_run": self.last_run,
        }


# Singleton
_indexer: WorkspaceKnowledgeIndexer | None = None


def get_indexer(workspace_dir: str | Path | None = None) -> WorkspaceKnowledgeIndexer:
    global _indexer
    if _indexer is None:
        _indexer = WorkspaceKnowledgeIndexer(workspace_dir)
    elif workspace_dir and _indexer.workspace is None:
        _indexer.workspace = workspace_dir
    return _indexer

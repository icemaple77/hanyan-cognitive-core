"""Bidirectional synchronisation between PostgreSQL and the QMD knowledge base.

The :class:`SyncEngine` keeps the durable :class:`~gateway.models.Memory` rows in
PostgreSQL and the human-readable markdown "Knowledge Document" (QMD) tree in
sync in *both* directions:

* **DB -> QMD** (:meth:`SyncEngine.sync_to_qmd`) regenerates the markdown tree
  from the database via :class:`~core.qmd_generator.QMDGenerator`. This is the
  canonical one-way projection of the source-of-truth database.
* **QMD -> DB** (:meth:`SyncEngine.sync_from_qmd`) walks the markdown tree,
  parses each document's YAML front matter + body, diffs it against the matching
  database row (keyed by the ``id`` embedded in the front matter) and merges any
  human edits back into PostgreSQL. Conflicts (both sides changed since the last
  generation) are detected and, by default, resolved conservatively.
* Optional **git integration** (:meth:`SyncEngine.git_integration`) captures a
  snapshot of the QMD tree after a sync pass using plain ``git`` subprocess
  commands.

Each merged change publishes a :class:`~core.event_bus.MemoryUpdated` event on
the shared :class:`~core.event_bus.EventBus` (when one is wired in) so other
components can react.

Configuration (all ``HCC_*`` env vars via :class:`~core.config.CoreSettings`):

* ``HCC_QMD_DIR`` -- root directory of the QMD tree.
* ``HCC_SYNC_INTERVAL`` -- seconds between passes when running as a loop.
* ``HCC_SYNC_GIT_ENABLED`` -- auto commit the QMD tree after each pass.
"""

from __future__ import annotations

import asyncio
import logging
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from core.config import CoreSettings, core_settings
from core.event_bus import EventBus, EventType
from core.qmd_generator import GenerationStats, QMDGenerator
from gateway.core.database import async_session
from gateway.models import Memory

logger = logging.getLogger(__name__)

# Front matter delimited by leading "---" lines, mirroring QMDGenerator output.
_FRONTMATTER_RE = re.compile(
    r"\A---[ \t]*\r?\n(?P<fm>.*?)\r?\n---[ \t]*\r?\n?",
    re.DOTALL,
)
# The tag footer appended by QMDGenerator: "\n---\n\nTags: #a #b".
_TAGS_FOOTER_RE = re.compile(
    r"\r?\n---\r?\n\r?\nTags:.*\Z",
    re.DOTALL,
)
# ATX H1 heading (the rendered title line).
_H1_RE = re.compile(r"^#\s+.*$", re.MULTILINE)

# Fields on a Memory that QMD -> DB sync is allowed to overwrite from markdown.
_MERGEABLE_FIELDS = ("content", "summary", "importance", "type", "status", "tags")


@dataclass
class QMDDocument:
    """A QMD markdown file parsed back into structured fields.

    Attributes
    ----------
    path:
        Filesystem path of the source markdown file.
    memory_id:
        The ``id`` recorded in the YAML front matter (``None`` if absent).
    frontmatter:
        The raw parsed front-matter mapping.
    title:
        The rendered H1 title (best-effort).
    summary:
        The blockquote summary line, if present.
    content:
        The reconstructed body content (title/summary/tag-footer stripped).
    tags:
        Tags as recorded in the front matter (authoritative over the footer).
    """

    path: Path
    memory_id: str | None
    frontmatter: dict[str, Any]
    title: str
    summary: str
    content: str
    tags: list[str]

    @property
    def importance(self) -> float | None:
        """Return the front-matter importance coerced to a float, if valid."""
        raw = self.frontmatter.get("importance")
        if isinstance(raw, (int, float)):
            return float(raw)
        return None

    @property
    def type(self) -> str | None:
        """Return the front-matter ``type`` as a string, if present."""
        value = self.frontmatter.get("type")
        return str(value) if value is not None else None

    @property
    def status(self) -> str | None:
        """Return the front-matter ``status`` as a string, if present."""
        value = self.frontmatter.get("status")
        return str(value) if value is not None else None

    def updated_at(self) -> datetime | None:
        """Return the front-matter ``updated_at`` parsed as a datetime."""
        return _parse_dt(self.frontmatter.get("updated_at"))


@dataclass
class SyncStats:
    """Summary of a bidirectional sync pass."""

    qmd_generated: GenerationStats | None = None
    from_qmd_scanned: int = 0
    from_qmd_updated: int = 0
    from_qmd_conflicts: int = 0
    from_qmd_skipped: int = 0
    from_qmd_unmatched: int = 0
    git_committed: bool = False
    updated_ids: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        """Return the stats as a plain dict (for logging / JSON)."""
        return {
            "qmd_generated": self.qmd_generated.as_dict()
            if self.qmd_generated
            else None,
            "from_qmd_scanned": self.from_qmd_scanned,
            "from_qmd_updated": self.from_qmd_updated,
            "from_qmd_conflicts": self.from_qmd_conflicts,
            "from_qmd_skipped": self.from_qmd_skipped,
            "from_qmd_unmatched": self.from_qmd_unmatched,
            "git_committed": self.git_committed,
            "updated_ids": self.updated_ids,
        }


def _parse_dt(value: Any) -> datetime | None:
    """Best-effort parse of an ISO-8601 (or ``datetime``) value into UTC-naive.

    QMDGenerator stores ``created_at``/``updated_at`` as ISO strings; YAML may
    also decode them straight to ``datetime`` objects. Returned datetimes are
    normalised to naive UTC to match the (naive) values stored by the ORM.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _normalise_tags(value: Any) -> list[str]:
    """Coerce a front-matter tags value into a clean list of strings."""
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str):
        parts = re.split(r"[,\s]+", value.strip())
        return [p for p in parts if p]
    return [str(value).strip()]


def parse_qmd_file(path: Path) -> QMDDocument | None:
    """Parse a single QMD markdown file back into a :class:`QMDDocument`.

    Reconstructs the body content by stripping the rendered H1 title line, the
    optional blockquote summary and the trailing ``Tags:`` footer that
    :class:`~core.qmd_generator.QMDGenerator` emits. Returns ``None`` when the
    file has no YAML front matter (e.g. a hand-written note or index README).

    Parameters
    ----------
    path:
        Path to the markdown file to parse.
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return None

    try:
        loaded = yaml.safe_load(match.group("fm"))
    except yaml.YAMLError:
        logger.warning("invalid front matter in %s", path)
        return None
    frontmatter: dict[str, Any] = loaded if isinstance(loaded, dict) else {}

    body = text[match.end():]

    # Strip the trailing "Tags:" footer so it does not pollute the content.
    body = _TAGS_FOOTER_RE.sub("", body)

    title = ""
    summary = ""
    content_lines: list[str] = []
    title_taken = False
    for line in body.splitlines():
        stripped = line.strip()
        if not title_taken and stripped.startswith("# "):
            title = stripped[2:].strip()
            title_taken = True
            continue
        if not content_lines and stripped.startswith(">"):
            # Blockquote summary line emitted right after the title.
            summary = stripped.lstrip(">").strip()
            continue
        content_lines.append(line)

    content = "\n".join(content_lines).strip()

    return QMDDocument(
        path=path,
        memory_id=(str(frontmatter["id"]) if frontmatter.get("id") else None),
        frontmatter=frontmatter,
        title=title,
        summary=summary,
        content=content,
        tags=_normalise_tags(frontmatter.get("tags")),
    )


def iter_qmd_files(root: Path) -> list[Path]:
    """Return every content ``*.md`` file under ``root`` (READMEs excluded)."""
    if not root.exists():
        return []
    return sorted(
        p
        for p in root.rglob("*.md")
        if p.is_file() and p.name.lower() != "readme.md"
    )


def index_qmd_files(qmd_dir: Path | str) -> dict[str, Path]:
    """Map ``memory_id -> markdown path`` by scanning the QMD tree.

    The scan reads each content document's front matter to recover the ``id``
    that ties a markdown file back to its :class:`~gateway.models.Memory`.

    Parameters
    ----------
    qmd_dir:
        Root ``HCC_QMD_DIR`` directory. The knowledge tree lives under
        ``<qmd_dir>/Knowledge``.
    """
    root = Path(qmd_dir).expanduser() / "Knowledge"
    mapping: dict[str, Path] = {}
    for path in iter_qmd_files(root):
        try:
            doc = parse_qmd_file(path)
        except OSError:
            logger.warning("cannot read QMD file %s", path, exc_info=True)
            continue
        if doc and doc.memory_id:
            mapping[doc.memory_id] = path
    return mapping


def resolve_qmd_path(qmd_dir: Path | str, memory_id: str) -> Path | None:
    """Return the QMD markdown path for ``memory_id`` or ``None`` if not found."""
    return index_qmd_files(qmd_dir).get(memory_id)


class SyncEngine:
    """Bidirectional synchroniser between PostgreSQL and the QMD tree.

    Parameters
    ----------
    settings:
        Optional :class:`CoreSettings`; defaults to the module singleton.
    session_factory:
        Optional async sessionmaker; defaults to the gateway ``async_session``.
        Injectable for testing.
    generator:
        Optional pre-built :class:`QMDGenerator`; one is created when omitted.
    event_bus:
        Optional connected :class:`EventBus`. When supplied, a
        :class:`~core.event_bus.MemoryUpdated` event is published for every row
        merged from QMD back into the database.
    """

    def __init__(
        self,
        *,
        settings: CoreSettings | None = None,
        session_factory: async_sessionmaker | None = None,
        generator: QMDGenerator | None = None,
        event_bus: EventBus | None = None,
    ) -> None:
        self._settings = settings or core_settings
        self._session_factory = session_factory or async_session
        self._generator = generator or QMDGenerator(
            settings=self._settings, session_factory=self._session_factory
        )
        self._event_bus = event_bus

    @property
    def qmd_dir(self) -> Path:
        """Return the configured ``HCC_QMD_DIR`` root directory."""
        return Path(self._settings.qmd_dir).expanduser()

    @property
    def root(self) -> Path:
        """Return the ``Knowledge`` root under :attr:`qmd_dir`."""
        return self.qmd_dir / "Knowledge"

    # ------------------------------------------------------------------
    # DB -> QMD
    # ------------------------------------------------------------------
    async def sync_to_qmd(self) -> GenerationStats:
        """Regenerate the QMD markdown tree from PostgreSQL.

        Delegates to :meth:`QMDGenerator.generate_all`, projecting the current
        database state onto the markdown knowledge base.

        Returns
        -------
        GenerationStats
            Statistics describing the generation pass.
        """
        logger.info("sync_to_qmd: regenerating QMD from PostgreSQL")
        stats = await self._generator.generate_all()
        logger.info("sync_to_qmd complete: %s", stats.as_dict())
        return stats

    # ------------------------------------------------------------------
    # QMD -> DB
    # ------------------------------------------------------------------
    async def sync_from_qmd(self) -> SyncStats:
        """Diff QMD documents against PostgreSQL and merge human edits back.

        Every content document in the tree is parsed and matched to a
        :class:`~gateway.models.Memory` by the ``id`` in its front matter. When
        the markdown body/metadata differs from the database row the change is
        merged into PostgreSQL. If the database row was itself modified since the
        markdown was last generated (a *conflict*) the change is skipped and the
        conflict counted, so no work is silently lost.

        Returns
        -------
        SyncStats
            Statistics describing the merge pass (``qmd_generated`` is ``None``).
        """
        stats = SyncStats()
        docs = [d for d in (parse_qmd_file(p) for p in iter_qmd_files(self.root)) if d]

        async with self._session_factory() as session:
            for doc in docs:
                stats.from_qmd_scanned += 1
                if not doc.memory_id:
                    stats.from_qmd_unmatched += 1
                    continue

                memory = await session.get(Memory, doc.memory_id)
                if memory is None:
                    stats.from_qmd_unmatched += 1
                    logger.debug("QMD id %s has no DB row: %s", doc.memory_id, doc.path)
                    continue

                changes = self._diff(memory, doc)
                if not changes:
                    continue

                if self._is_conflict(memory, doc):
                    stats.from_qmd_conflicts += 1
                    logger.warning(
                        "sync_from_qmd conflict for %s (%s): DB newer than QMD; "
                        "keeping DB, skipping fields %s",
                        memory.id,
                        doc.path.name,
                        sorted(changes),
                    )
                    stats.from_qmd_skipped += 1
                    continue

                self._apply(memory, changes)
                memory.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
                stats.from_qmd_updated += 1
                stats.updated_ids.append(memory.id)
                logger.info(
                    "sync_from_qmd merged %s from %s (fields=%s)",
                    memory.id,
                    doc.path.name,
                    sorted(changes),
                )

            if stats.from_qmd_updated:
                await session.commit()

        # Publish events after the transaction has committed.
        for memory_id in stats.updated_ids:
            await self._publish_updated(memory_id)

        logger.info("sync_from_qmd complete: %s", stats.as_dict())
        return stats

    def _diff(self, memory: Memory, doc: QMDDocument) -> dict[str, Any]:
        """Return the set of mergeable fields where ``doc`` differs from ``memory``.

        Only the fields in :data:`_MERGEABLE_FIELDS` are considered, and each is
        compared using normalised values so cosmetic differences (whitespace,
        tag ordering) do not trigger spurious updates.
        """
        changes: dict[str, Any] = {}

        db_content = (memory.content or "").strip()
        if doc.content and doc.content != db_content:
            changes["content"] = doc.content

        db_summary = (memory.summary or "").strip()
        doc_summary = doc.summary.strip()
        if doc_summary and doc_summary != db_summary:
            changes["summary"] = doc_summary

        if doc.importance is not None and not _floats_equal(
            doc.importance, memory.importance
        ):
            changes["importance"] = doc.importance

        if doc.type and doc.type != (memory.type or ""):
            changes["type"] = doc.type

        if doc.status and doc.status != (memory.status or ""):
            changes["status"] = doc.status

        if doc.tags and doc.tags != list(memory.tags or []):
            changes["tags"] = doc.tags

        return changes

    @staticmethod
    def _apply(memory: Memory, changes: dict[str, Any]) -> None:
        """Apply the diff ``changes`` onto the ``memory`` ORM instance."""
        for field_name, value in changes.items():
            setattr(memory, field_name, value)

    @staticmethod
    def _is_conflict(memory: Memory, doc: QMDDocument) -> bool:
        """Return ``True`` when both DB and QMD changed since last generation.

        The markdown front matter records the ``updated_at`` at generation time.
        If the live database ``updated_at`` is strictly newer, the row was
        touched after the markdown was produced, so a human edit to the markdown
        would clobber a fresher database write -- that is treated as a conflict.
        """
        doc_updated = doc.updated_at()
        if doc_updated is None or memory.updated_at is None:
            return False
        return memory.updated_at > doc_updated

    async def _publish_updated(self, memory_id: str) -> None:
        """Publish a ``MemoryUpdated`` event for ``memory_id`` if a bus is set."""
        if self._event_bus is None:
            return
        try:
            await self._event_bus.publish_event(
                EventType.MEMORY_UPDATED,
                {"id": memory_id, "origin": "sync_from_qmd"},
                source="sync_engine",
            )
        except Exception:  # noqa: BLE001 - never let event publishing break sync
            logger.exception("failed to publish MemoryUpdated for %s", memory_id)

    # ------------------------------------------------------------------
    # Combined passes
    # ------------------------------------------------------------------
    async def sync_once(self, *, to_qmd: bool = True, from_qmd: bool = True) -> SyncStats:
        """Run one full bidirectional pass.

        The QMD -> DB merge runs *first* so human edits are captured, then the
        DB -> QMD projection regenerates the tree (normalising formatting and
        picking up any other database changes). Finally, git integration commits
        the result when enabled.

        Parameters
        ----------
        to_qmd:
            Run the DB -> QMD generation step.
        from_qmd:
            Run the QMD -> DB merge step.

        Returns
        -------
        SyncStats
            Combined statistics for the pass.
        """
        stats = SyncStats()
        if from_qmd:
            merged = await self.sync_from_qmd()
            stats.from_qmd_scanned = merged.from_qmd_scanned
            stats.from_qmd_updated = merged.from_qmd_updated
            stats.from_qmd_conflicts = merged.from_qmd_conflicts
            stats.from_qmd_skipped = merged.from_qmd_skipped
            stats.from_qmd_unmatched = merged.from_qmd_unmatched
            stats.updated_ids = merged.updated_ids

        if to_qmd:
            stats.qmd_generated = await self.sync_to_qmd()

        if self._settings.sync_git_enabled:
            stats.git_committed = self.git_integration(stats)

        return stats

    async def run(
        self, *, interval: int | None = None, once: bool = False
    ) -> None:
        """Run the sync engine, once or as a periodic loop.

        Parameters
        ----------
        interval:
            Override for ``HCC_SYNC_INTERVAL`` (seconds between passes).
        once:
            When ``True``, perform a single pass and return.
        """
        if once:
            await self.sync_once()
            return

        wait = interval if interval is not None else self._settings.sync_interval
        logger.info("SyncEngine loop starting (interval=%ss)", wait)
        while True:
            try:
                await self.sync_once()
            except Exception:  # noqa: BLE001 - keep the loop alive
                logger.exception("sync pass failed")
            await asyncio.sleep(wait)

    # ------------------------------------------------------------------
    # Git integration (subprocess, not gitpython)
    # ------------------------------------------------------------------
    def git_integration(self, stats: SyncStats | None = None) -> bool:
        """Stage and commit the QMD tree with plain ``git`` subprocess commands.

        Initialises a repository under ``HCC_QMD_DIR`` on first use. Returns
        ``True`` when a commit was actually created, ``False`` when there was
        nothing to commit or git is unavailable.

        Parameters
        ----------
        stats:
            Optional :class:`SyncStats` used to enrich the commit message.
        """
        repo = self.qmd_dir
        try:
            repo.mkdir(parents=True, exist_ok=True)
            if not (repo / ".git").exists():
                self._run_git(["init"], repo)
            self._run_git(["add", "-A"], repo)
            diff = subprocess.run(
                ["git", "diff", "--cached", "--quiet"], cwd=str(repo)
            )
            if diff.returncode == 0:
                logger.info("sync git: no changes to commit")
                return False
            self._run_git(["commit", "-m", self._commit_message(stats)], repo)
            logger.info("sync git: committed QMD tree")
            return True
        except (subprocess.CalledProcessError, OSError):
            logger.exception("sync git commit failed")
            return False

    @staticmethod
    def _commit_message(stats: SyncStats | None) -> str:
        """Build a commit message summarising the sync pass."""
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if stats is not None:
            return (
                f"chore(sync): bidirectional sync {stamp} "
                f"(merged {stats.from_qmd_updated}, "
                f"conflicts {stats.from_qmd_conflicts})"
            )
        return f"chore(sync): bidirectional sync {stamp}"

    @staticmethod
    def _run_git(args: Iterable[str], cwd: Path) -> None:
        """Run a git command in ``cwd``, raising :class:`CalledProcessError`."""
        subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            check=True,
            capture_output=True,
            text=True,
        )


def _floats_equal(a: float, b: float | None, *, tol: float = 1e-6) -> bool:
    """Return ``True`` if two floats are equal within ``tol`` (``None`` -> unequal)."""
    if b is None:
        return False
    return abs(a - b) <= tol


async def _amain() -> None:
    """Async entrypoint: run a single bidirectional sync pass and print stats."""
    engine = SyncEngine()
    stats = await engine.sync_once()
    print("Sync complete:")
    for key, value in stats.as_dict().items():
        print(f"  {key}: {value}")


def main() -> None:
    """CLI entrypoint (``python -m core.sync_engine``)."""
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
    )
    asyncio.run(_amain())


if __name__ == "__main__":
    import sys
    if "--watch" in sys.argv:
        watch_qmd()
    else:
        main()


def watch_qmd(callback: callable | None = None) -> None:
    """Watch QMD directory for changes and sync back to DB in real time.

    Uses watchdog ``Observer`` to detect file modification / creation events
    on ``*.md`` files under the configured ``HCC_QMD_DIR`` and merges them
    back into PostgreSQL immediately.

    Requires the ``watchdog`` package (``pip install watchdog``).
    """
    import time

    try:
        from watchdog.observers import Observer
        from watchdog.events import FileSystemEventHandler
    except ImportError:
        print("watchdog not installed. Run: pip install watchdog")
        return

    qmd_dir = Path(core_settings.qmd_dir).expanduser()

    class _QMDHandler(FileSystemEventHandler):
        def on_modified(self, event):
            if not event.src_path.endswith(".md"):
                return
            print(f"[watch] modified: {event.src_path}")
            asyncio.run(_sync_single_file(event.src_path))

        def on_created(self, event):
            if not event.src_path.endswith(".md"):
                return
            print(f"[watch] created: {event.src_path}")
            asyncio.run(_sync_single_file(event.src_path))

    observer = Observer()
    observer.schedule(_QMDHandler(), str(qmd_dir), recursive=True)
    observer.start()
    print(f"[watch] Watching {qmd_dir} for .md changes...")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()


async def _sync_single_file(filepath: str) -> None:
    """Parse a single QMD file at ``filepath`` and merge its changes into DB."""
    path = Path(filepath)
    if not path.exists() or not path.is_file():
        return
    engine = SyncEngine()
    doc = parse_qmd_file(path)
    if not doc or not doc.memory_id:
        return
    async with engine._session_factory() as session:
        memory = await session.get(Memory, doc.memory_id)
        if memory is None:
            return
        changes = engine._diff(memory, doc)
        if not changes:
            return
        engine._apply(memory, changes)
        memory.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
        await session.commit()
        print(f"[watch] synced {doc.memory_id} from {path.name}: {sorted(changes)}")

"""Directory watcher / scan loop for the HCC file scanner.

The watcher walks the configured directories, matches files against the glob
patterns, hashes each candidate with SHA-256 and consults a small SQLite state
database to decide whether the file is new, changed or unchanged. New and
changed files are parsed and handed to the :class:`~scanner.absorber.Absorber`.

State is persisted with :mod:`aiosqlite` so re-runs skip already-absorbed,
unmodified files.

Beyond the original one-shot / polling scan the watcher gains v2 capabilities:

* ``--sync`` -- after absorbing, run a full bidirectional PostgreSQL<->QMD sync
  via :class:`~core.sync_engine.SyncEngine` so the markdown knowledge tree is
  regenerated (and any human QMD edits are merged back into the database).
* QMD tracking -- once a QMD tree exists, the state DB records which QMD
  markdown file each scanned source file ultimately produced (via the memory
  id embedded in the QMD front matter).
* ``--watch`` -- continuously monitor the scan dirs for changes using
  :mod:`watchdog` (inotify/FSEvents), debouncing bursts of edits into scans.
"""

from __future__ import annotations

import argparse
import asyncio
import fnmatch
import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import aiosqlite

from scanner.absorber import Absorber
from scanner.config import ScannerSettings, load_settings
from scanner.parser import parse_file

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids importing DB layer
    from core.sync_engine import SyncEngine

logger = logging.getLogger("hcc.scanner")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS scanned_files (
    path TEXT PRIMARY KEY,
    hash TEXT NOT NULL,
    last_modified REAL NOT NULL,
    status TEXT NOT NULL,
    memory_id TEXT,
    qmd_path TEXT,
    scanned_at REAL NOT NULL
);
"""


@dataclass
class ScanStats:
    """Aggregate counters describing the result of a scan pass."""

    scanned: int = 0
    new: int = 0
    changed: int = 0
    unchanged: int = 0
    stored: int = 0
    failed: int = 0

    def as_dict(self) -> dict[str, int]:
        """Return the stats as a plain dictionary for logging/serialisation."""
        return {
            "scanned": self.scanned,
            "new": self.new,
            "changed": self.changed,
            "unchanged": self.unchanged,
            "stored": self.stored,
            "failed": self.failed,
        }


def hash_file(path: Path, *, chunk_size: int = 65536) -> str:
    """Return the SHA-256 hex digest of the file at *path*.

    Args:
        path: File to hash.
        chunk_size: Read buffer size in bytes.

    Returns:
        Lower-case hexadecimal SHA-256 digest.
    """
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


class Watcher:
    """Scan directories for markdown files and absorb new/changed ones."""

    def __init__(
        self,
        settings: ScannerSettings,
        *,
        sync_engine: "SyncEngine | None" = None,
    ) -> None:
        """Initialise the watcher.

        Args:
            settings: Fully-populated scanner settings.
            sync_engine: Optional :class:`~core.sync_engine.SyncEngine`. When
                ``settings.sync`` (or ``settings.generate_qmd``) is enabled a
                default engine is created lazily if none is injected. Injectable
                for testing.
        """
        self._settings = settings
        self._sync_engine = sync_engine

    # -- state database -------------------------------------------------

    async def _connect(self) -> aiosqlite.Connection:
        """Open the SQLite state DB, creating the file and schema if needed."""
        db_path = self._settings.state_db
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(db_path)
        conn.row_factory = aiosqlite.Row
        await conn.executescript(_SCHEMA)
        await self._migrate(conn)
        await conn.commit()
        return conn

    @staticmethod
    async def _migrate(conn: aiosqlite.Connection) -> None:
        """Apply lightweight, idempotent schema migrations to older state DBs."""
        async with conn.execute("PRAGMA table_info(scanned_files)") as cursor:
            columns = {row["name"] for row in await cursor.fetchall()}
        if "qmd_path" not in columns:
            await conn.execute("ALTER TABLE scanned_files ADD COLUMN qmd_path TEXT")

    async def _get_record(
        self, conn: aiosqlite.Connection, path: str
    ) -> aiosqlite.Row | None:
        """Fetch the stored record for *path*, or ``None`` if unknown."""
        async with conn.execute(
            "SELECT path, hash, last_modified, status, memory_id, qmd_path "
            "FROM scanned_files WHERE path = ?",
            (path,),
        ) as cursor:
            return await cursor.fetchone()

    async def _upsert_record(
        self,
        conn: aiosqlite.Connection,
        *,
        path: str,
        file_hash: str,
        last_modified: float,
        status: str,
        memory_id: str | None,
        scanned_at: float,
        qmd_path: str | None = None,
    ) -> None:
        """Insert or update the state record for a scanned file."""
        await conn.execute(
            """
            INSERT INTO scanned_files
                (path, hash, last_modified, status, memory_id, qmd_path, scanned_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                hash=excluded.hash,
                last_modified=excluded.last_modified,
                status=excluded.status,
                memory_id=excluded.memory_id,
                qmd_path=excluded.qmd_path,
                scanned_at=excluded.scanned_at
            """,
            (path, file_hash, last_modified, status, memory_id, qmd_path, scanned_at),
        )
        await conn.commit()

    async def _update_qmd_paths(
        self, conn: aiosqlite.Connection, mapping: dict[str, Path]
    ) -> int:
        """Record which QMD file each stored memory produced.

        Args:
            conn: Open state-database connection.
            mapping: ``memory_id -> QMD markdown path`` mapping, typically from
                :func:`core.sync_engine.index_qmd_files`.

        Returns:
            The number of state rows updated with a QMD path.
        """
        updated = 0
        for memory_id, qmd_path in mapping.items():
            cursor = await conn.execute(
                "UPDATE scanned_files SET qmd_path = ? WHERE memory_id = ?",
                (str(qmd_path), memory_id),
            )
            updated += cursor.rowcount or 0
        await conn.commit()
        return updated

    # -- file discovery -------------------------------------------------

    def _matches_pattern(self, name: str) -> bool:
        """Return ``True`` if *name* matches any configured glob pattern."""
        return any(fnmatch.fnmatch(name, pat) for pat in self._settings.patterns)

    def iter_files(self) -> list[Path]:
        """Yield every file under the configured dirs matching the patterns.

        Excluded directory names are pruned from the walk. Missing scan dirs
        are logged and skipped rather than raising.

        Returns:
            A sorted list of matching file paths.
        """
        excluded = set(self._settings.exclude)
        found: list[Path] = []

        for root in self._settings.scan_dirs():
            if not root.exists():
                logger.warning("scan dir does not exist: %s", root)
                continue
            for path in root.rglob("*"):
                if not path.is_file():
                    continue
                # Skip files living under any excluded directory component.
                if excluded.intersection(path.parts):
                    continue
                if self._matches_pattern(path.name):
                    found.append(path)

        return sorted(found)

    # -- scanning -------------------------------------------------------

    async def scan_once(self) -> ScanStats:
        """Perform a single scan pass over all configured directories.

        Returns:
            A :class:`ScanStats` summary of the pass.
        """
        stats = ScanStats()
        files = self.iter_files()
        logger.info(
            "scan starting: %d candidate file(s)%s",
            len(files),
            " [dry-run]" if self._settings.dry_run else "",
        )

        conn = await self._connect()
        try:
            async with Absorber(self._settings) as absorber:
                for path in files:
                    stats.scanned += 1
                    await self._process_file(conn, absorber, path, stats)

            # After absorbing, optionally regenerate the QMD tree and track
            # which QMD file each scanned source produced.
            if not self._settings.dry_run and (
                self._settings.sync or self._settings.generate_qmd
            ):
                await self._post_scan_sync(conn, stats)
        finally:
            await conn.close()

        logger.info("scan complete: %s", stats.as_dict())
        return stats

    def _ensure_sync_engine(self) -> "SyncEngine":
        """Return the injected SyncEngine or lazily build a default one."""
        if self._sync_engine is None:
            from core.sync_engine import SyncEngine

            self._sync_engine = SyncEngine()
        return self._sync_engine

    async def _post_scan_sync(
        self, conn: aiosqlite.Connection, stats: ScanStats
    ) -> None:
        """Run the DB<->QMD sync after a scan and record QMD file mappings.

        With ``--sync`` a full bidirectional pass runs (merging any human QMD
        edits back to the database and regenerating the tree). With only
        ``generate_qmd`` set, a one-way DB -> QMD regeneration runs. Either way
        the state DB is updated so each source file points at the QMD markdown
        file its memory produced.
        """
        engine = self._ensure_sync_engine()
        try:
            if self._settings.sync:
                await engine.sync_once()
            else:
                await engine.sync_to_qmd()

            from core.sync_engine import index_qmd_files

            mapping = index_qmd_files(engine.qmd_dir)
            linked = await self._update_qmd_paths(conn, mapping)
            logger.info("sync complete: linked %d file(s) to QMD entries", linked)
        except Exception:  # noqa: BLE001 - sync failures must not abort a scan
            logger.exception("post-scan sync failed")

    async def _process_file(
        self,
        conn: aiosqlite.Connection,
        absorber: Absorber,
        path: Path,
        stats: ScanStats,
    ) -> None:
        """Process a single file: hash, diff against state, absorb, record.

        All per-file exceptions are caught and counted as failures so one bad
        file cannot abort the scan.
        """
        key = str(path.resolve())
        try:
            file_hash = hash_file(path)
            mtime = path.stat().st_mtime
        except OSError as exc:
            stats.failed += 1
            logger.error("cannot read %s: %s", path, exc)
            return

        record = await self._get_record(conn, key)
        if record is not None and record["hash"] == file_hash and record["status"] == "stored":
            stats.unchanged += 1
            logger.debug("unchanged: %s", path)
            return

        is_new = record is None
        if is_new:
            stats.new += 1
        else:
            stats.changed += 1

        try:
            doc = parse_file(path)
        except Exception as exc:  # noqa: BLE001 - never let parsing abort the scan
            stats.failed += 1
            logger.error("failed to parse %s: %s", path, exc)
            await self._upsert_record(
                conn,
                path=key,
                file_hash=file_hash,
                last_modified=mtime,
                status="parse_error",
                memory_id=None,
                scanned_at=_now(),
            )
            return

        extra_tags = [path.parent.name] if path.parent.name else None
        result = await absorber.absorb(doc, extra_tags=extra_tags)

        if result.ok:
            stats.stored += 1
            status = "dry_run" if result.dry_run else "stored"
            verb = "would store" if result.dry_run else "stored"
            logger.info("%s: %s -> %s", verb, path, result.memory_id or "-")
        else:
            stats.failed += 1
            status = "error"
            logger.error("failed to store %s: %s", path, result.error)

        await self._upsert_record(
            conn,
            path=key,
            file_hash=file_hash,
            last_modified=mtime,
            status=status,
            memory_id=result.memory_id,
            qmd_path=result.qmd_path,
            scanned_at=_now(),
        )

    async def run(self) -> None:
        """Run the scanner.

        Dispatches on the configured mode:

        * ``watch`` -- monitor the scan dirs continuously with :mod:`watchdog`.
        * ``once`` -- perform a single scan pass and return.
        * otherwise -- loop forever, sleeping ``interval`` seconds between passes.
        """
        if self._settings.watch:
            await self.watch()
            return

        if self._settings.once:
            await self.scan_once()
            return

        logger.info("scanner loop starting (interval=%ss)", self._settings.interval)
        while True:
            try:
                await self.scan_once()
            except Exception as exc:  # noqa: BLE001 - keep the loop alive
                logger.exception("scan pass failed: %s", exc)
            await asyncio.sleep(self._settings.interval)

    async def watch(self, *, debounce: float = 2.0) -> None:
        """Continuously watch the scan dirs and re-scan on changes.

        Uses :mod:`watchdog` to observe filesystem events (created/modified/
        moved/deleted) beneath the configured directories. Matching events are
        coalesced with a short debounce window so a burst of edits triggers a
        single :meth:`scan_once` pass. An initial pass runs immediately so the
        baseline state is captured before watching begins.

        Args:
            debounce: Seconds to wait for the event stream to settle before
                triggering a scan pass.

        Raises:
            RuntimeError: If ``watchdog`` is not installed.
        """
        try:
            from watchdog.events import FileSystemEvent, FileSystemEventHandler
            from watchdog.observers import Observer
        except ImportError as exc:  # pragma: no cover - depends on optional dep
            raise RuntimeError(
                "watch mode requires the 'watchdog' package; install it with "
                "`pip install watchdog` or `uv sync`."
            ) from exc

        loop = asyncio.get_running_loop()
        wake = asyncio.Event()

        class _Handler(FileSystemEventHandler):
            """Signal the async loop when a matching markdown file changes."""

            def __init__(self, matches) -> None:
                self._matches = matches

            def _on_change(self, event: "FileSystemEvent") -> None:
                if event.is_directory:
                    return
                for attr in ("src_path", "dest_path"):
                    raw = getattr(event, attr, None)
                    if raw and self._matches(Path(str(raw)).name):
                        loop.call_soon_threadsafe(wake.set)
                        return

            on_created = _on_change
            on_modified = _on_change
            on_moved = _on_change
            on_deleted = _on_change

        observer = Observer()
        handler = _Handler(self._matches_pattern)
        watched = 0
        for root in self._settings.scan_dirs():
            if root.exists():
                observer.schedule(handler, str(root), recursive=True)
                watched += 1
            else:
                logger.warning("watch dir does not exist: %s", root)

        if watched == 0:
            logger.error("no existing scan dirs to watch")
            return

        observer.start()
        logger.info(
            "watch mode started on %d dir(s); running initial scan", watched
        )
        try:
            await self.scan_once()  # Baseline pass.
            while True:
                await wake.wait()
                # Debounce: let a burst of edits settle before scanning.
                await asyncio.sleep(debounce)
                wake.clear()
                logger.info("change detected; rescanning")
                try:
                    await self.scan_once()
                except Exception:  # noqa: BLE001 - keep watching after a failure
                    logger.exception("scan pass failed")
        except asyncio.CancelledError:  # pragma: no cover - shutdown path
            raise
        finally:
            observer.stop()
            observer.join()
            logger.info("watch mode stopped")


def _now() -> float:
    """Return the current wall-clock time as a Unix timestamp."""
    import time

    return time.time()


def main() -> None:
    """Console entry point: configure logging and run the watcher."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = load_settings()
    if not settings.dirs:
        logger.error(
            "no scan directories configured; set HCC_SCANNER_DIRS "
            "(comma-separated paths)"
        )
        raise SystemExit(2)

    watcher = Watcher(settings)
    try:
        asyncio.run(watcher.run())
    except KeyboardInterrupt:  # pragma: no cover - interactive interrupt
        logger.info("scanner stopped")


if __name__ == "__main__":
    main()

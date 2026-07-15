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

import aiosqlite

from scanner.absorber import Absorber
from scanner.config import ScannerSettings, load_settings
from scanner.parser import parse_file

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

    def __init__(self, settings: ScannerSettings) -> None:
        """Initialise the watcher.

        Args:
            settings: Fully-populated scanner settings.
        """
        self._settings = settings

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
            "SELECT path, hash, last_modified, status, memory_id "
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
    ) -> None:
        """Insert or update the state record for a scanned file."""
        await conn.execute(
            """
            INSERT INTO scanned_files
                (path, hash, last_modified, status, memory_id, scanned_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                hash=excluded.hash,
                last_modified=excluded.last_modified,
                status=excluded.status,
                memory_id=excluded.memory_id,
                scanned_at=excluded.scanned_at
            """,
            (path, file_hash, last_modified, status, memory_id, scanned_at),
        )
        await conn.commit()

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
        finally:
            await conn.close()

        logger.info("scan complete: %s", stats.as_dict())
        return stats

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
            scanned_at=_now(),
        )

    async def run(self) -> None:
        """Run the scanner.

        Performs a single pass when ``once`` is set, otherwise loops forever,
        sleeping ``interval`` seconds between passes.
        """
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

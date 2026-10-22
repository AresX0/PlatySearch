"""Publish consistent SQLite snapshots without overwriting the last good copy."""

from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
import threading
from contextlib import closing
from pathlib import Path

_RESERVE_BYTES = 64 * 1024 * 1024
_LOCK = threading.Lock()


def persist_database(source: Path, directory: Path) -> bool:
    """Atomically publish a validated backup, including committed WAL contents.

    A failed backup must never truncate the authoritative persistent database.
    Keep the staging file on the destination filesystem so replacement is atomic.
    """
    if not directory.is_dir() or not source.is_file():
        return False
    destination = directory / "platysearch.db"
    if source.resolve() == destination.resolve():
        raise ValueError("Live and persistent database paths must be different")

    with _LOCK:
        staging: Path | None = None
        try:
            with closing(sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro", uri=True)) as live:
                page_size = live.execute("PRAGMA page_size").fetchone()[0]
                page_count = live.execute("PRAGMA page_count").fetchone()[0]
                required = page_size * page_count + _RESERVE_BYTES
                free = shutil.disk_usage(directory).free
                if free < required:
                    raise OSError(
                        f"Persistent storage needs {required} free bytes for a safe "
                        f"snapshot; only {free} available. Existing database preserved."
                    )
                fd, name = tempfile.mkstemp(prefix=".platysearch-", suffix=".db", dir=directory)
                staging = Path(name)
                os.close(fd)

                def check_space(status: int, remaining: int, total: int) -> None:
                    if shutil.disk_usage(directory).free < _RESERVE_BYTES:
                        raise OSError("Persistent storage reserve exhausted during backup")

                with closing(sqlite3.connect(staging)) as backup:
                    live.backup(backup, pages=1024, progress=check_space)
                    # Publish a standalone file, not a database requiring WAL sidecars.
                    backup.execute("PRAGMA journal_mode=DELETE")
                    results = backup.execute("PRAGMA quick_check").fetchall()
                    if results != [("ok",)]:
                        raise sqlite3.DatabaseError("Persistent snapshot failed quick_check")
            with staging.open("rb") as snapshot:
                os.fsync(snapshot.fileno())
            os.replace(staging, destination)
            return True
        finally:
            if staging is not None:
                staging.unlink(missing_ok=True)
                for suffix in ("-journal", "-wal", "-shm"):
                    Path(f"{staging}{suffix}").unlink(missing_ok=True)
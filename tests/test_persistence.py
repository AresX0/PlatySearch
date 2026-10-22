"""SQLite persistence safety tests; only the Python standard library is needed."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from platysearch.persistence import persist_database


class PersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "live.db"
        self.directory = self.root / "persistent"
        self.directory.mkdir()
        self.destination = self.directory / "platysearch.db"
        self.destination.write_bytes(b"last known good copy")
        self.original = self.destination.read_bytes()
        connection = sqlite3.connect(self.source)
        connection.execute("CREATE TABLE pages (body TEXT)")
        connection.execute("INSERT INTO pages VALUES ('original content')")
        connection.commit()
        connection.close()

    def assert_preserved(self) -> None:
        self.assertEqual(self.destination.read_bytes(), self.original)
        self.assertEqual(list(self.directory.iterdir()), [self.destination])

    def test_successful_snapshot(self) -> None:
        self.assertTrue(persist_database(self.source, self.directory))
        connection = sqlite3.connect(self.destination)
        try:
            self.assertEqual(connection.execute("PRAGMA quick_check").fetchall(), [("ok",)])
            self.assertEqual(
                connection.execute("SELECT body FROM pages").fetchall(),
                [("original content",)],
            )
        finally:
            connection.close()
        self.assertEqual(list(self.directory.iterdir()), [self.destination])

    def test_includes_committed_wal(self) -> None:
        connection = sqlite3.connect(self.source)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("INSERT INTO pages VALUES ('committed WAL content')")
            connection.commit()
            self.assertTrue(Path(f"{self.source}-wal").is_file())
            persist_database(self.source, self.directory)
            snapshot = sqlite3.connect(self.destination)
            try:
                self.assertEqual(snapshot.execute("SELECT COUNT(*) FROM pages").fetchone(), (2,))
                self.assertEqual(snapshot.execute("PRAGMA journal_mode").fetchone(), ("delete",))
            finally:
                snapshot.close()
        finally:
            connection.close()

    def test_full_share_preserves_original(self) -> None:
        with patch(
            "platysearch.persistence.shutil.disk_usage", return_value=SimpleNamespace(free=0),
        ):
            with self.assertRaises(OSError):
                persist_database(self.source, self.directory)
        self.assert_preserved()

    def test_space_exhausted_during_backup_preserves_original(self) -> None:
        with patch(
            "platysearch.persistence.shutil.disk_usage",
            side_effect=[SimpleNamespace(free=1024**3), SimpleNamespace(free=0)],
        ):
            with self.assertRaises(OSError):
                persist_database(self.source, self.directory)
        self.assert_preserved()

    def test_replace_failure_preserves_original(self) -> None:
        with patch("platysearch.persistence.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                persist_database(self.source, self.directory)
        self.assert_preserved()

    def test_corrupt_source_preserves_original(self) -> None:
        self.source.write_bytes(b"truncated SQLite database")
        with self.assertRaises(sqlite3.DatabaseError):
            persist_database(self.source, self.directory)
        self.assert_preserved()

    def test_no_source_or_persistent_directory(self) -> None:
        self.assertFalse(persist_database(self.root / "missing.db", self.directory))
        self.assertFalse(persist_database(self.source, self.root / "missing"))
        self.assert_preserved()

    def test_source_cannot_be_destination(self) -> None:
        with self.assertRaises(ValueError):
            persist_database(self.destination, self.directory)
        self.assert_preserved()
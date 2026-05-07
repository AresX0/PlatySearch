#!/usr/bin/env python3
"""Standalone DB maintenance — stdlib only, no platysearch deps.

Usage:
    python3 dedupe_standalone.py /home/data/platysearch.db [--dry-run]

Designed to run from the Azure App Service Kudu sidecar against the
SQLite DB on the persistent /home mount. Uses /temp for SQLite tempfiles
since /home only has ~2 GB free and a full VACUUM may need several GB.
"""

from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import sys
import time
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("dedupe")


def scalar(db: sqlite3.Connection, sql: str) -> int:
    row = db.execute(sql).fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def file_size_mb(path: Path) -> float:
    return path.stat().st_size / (1024 * 1024) if path.exists() else 0.0


def report(db: sqlite3.Connection, db_path: Path, label: str) -> None:
    pages = scalar(db, "SELECT COUNT(*) FROM pages")
    pages_with_hash = scalar(
        db, "SELECT COUNT(*) FROM pages WHERE content_hash IS NOT NULL"
    )
    distinct_hashes = scalar(
        db,
        "SELECT COUNT(DISTINCT content_hash) FROM pages "
        "WHERE content_hash IS NOT NULL",
    )
    dup_pages = pages_with_hash - distinct_hashes
    raw_html_rows = scalar(
        db, "SELECT COUNT(*) FROM pages WHERE raw_html IS NOT NULL"
    )
    raw_html_nonwiki = scalar(
        db,
        "SELECT COUNT(*) FROM pages WHERE raw_html IS NOT NULL "
        "AND domain != 'en.wikipedia.org'",
    )
    links = scalar(db, "SELECT COUNT(*) FROM links")
    distinct_links = scalar(
        db,
        "SELECT COUNT(*) FROM (SELECT 1 FROM links GROUP BY source_id, target_url)",
    )
    dup_links = links - distinct_links
    images = scalar(db, "SELECT COUNT(*) FROM page_images")
    distinct_images = scalar(
        db,
        "SELECT COUNT(*) FROM (SELECT 1 FROM page_images GROUP BY page_id, src_url)",
    )
    dup_images = images - distinct_images
    postings = scalar(db, "SELECT COUNT(*) FROM postings")
    queue = scalar(db, "SELECT COUNT(*) FROM crawl_queue")
    size = file_size_mb(db_path)

    log.info("==== DB report (%s) ====", label)
    log.info("file size:                 %.1f MB", size)
    log.info("pages:                     %d", pages)
    log.info("  with content_hash:       %d", pages_with_hash)
    log.info("  distinct hashes:         %d", distinct_hashes)
    log.info("  duplicate hash rows:     %d", dup_pages)
    log.info("  rows with raw_html:      %d", raw_html_rows)
    log.info("  raw_html non-wiki rows:  %d", raw_html_nonwiki)
    log.info("links:                     %d  (duplicates: %d)", links, dup_links)
    log.info("page_images:               %d  (duplicates: %d)", images, dup_images)
    log.info("postings:                  %d", postings)
    log.info("crawl_queue:               %d", queue)


def dedupe_pages(db: sqlite3.Connection) -> int:
    rows = db.execute(
        """
        SELECT p.id AS dup_id, k.keep_id
        FROM pages p
        JOIN (
            SELECT content_hash, MIN(id) AS keep_id
            FROM pages
            WHERE content_hash IS NOT NULL AND content_hash != ''
            GROUP BY content_hash
            HAVING COUNT(*) > 1
        ) k ON k.content_hash = p.content_hash
        WHERE p.id != k.keep_id
        """
    ).fetchall()
    if not rows:
        log.info("dedupe_pages: no duplicate-hash pages")
        return 0

    log.info("dedupe_pages: rewiring %d duplicate pages", len(rows))
    db.execute("BEGIN")
    try:
        for dup_id, keep_id in rows:
            db.execute(
                "UPDATE OR IGNORE links SET source_id = ? WHERE source_id = ?",
                (keep_id, dup_id),
            )
            db.execute("DELETE FROM links WHERE source_id = ?", (dup_id,))
            db.execute(
                "UPDATE OR IGNORE postings SET page_id = ? WHERE page_id = ?",
                (keep_id, dup_id),
            )
            db.execute("DELETE FROM postings WHERE page_id = ?", (dup_id,))
            db.execute(
                "UPDATE OR IGNORE page_images SET page_id = ? WHERE page_id = ?",
                (keep_id, dup_id),
            )
            db.execute("DELETE FROM page_images WHERE page_id = ?", (dup_id,))
            db.execute("DELETE FROM page_scores WHERE page_id = ?", (dup_id,))

        ids = [d for d, _ in rows]
        # Chunk in case the IN-list is huge.
        for i in range(0, len(ids), 500):
            chunk = ids[i : i + 500]
            placeholders = ",".join("?" * len(chunk))
            db.execute(
                f"DELETE FROM pages WHERE id IN ({placeholders})", chunk  # noqa: S608
            )
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise
    log.info("dedupe_pages: deleted %d rows", len(rows))
    return len(rows)


def dedupe_links(db: sqlite3.Connection) -> int:
    log.info("dedupe_links: rebuilding...")
    cur = db.execute(
        """
        DELETE FROM links
        WHERE id NOT IN (
            SELECT MIN(id) FROM links GROUP BY source_id, target_url
        )
        """
    )
    deleted = cur.rowcount
    db.commit()
    log.info("dedupe_links: deleted %d rows", deleted)
    return deleted


def dedupe_images(db: sqlite3.Connection) -> int:
    log.info("dedupe_images: rebuilding...")
    cur = db.execute(
        """
        DELETE FROM page_images
        WHERE id NOT IN (
            SELECT MIN(id) FROM page_images GROUP BY page_id, src_url
        )
        """
    )
    deleted = cur.rowcount
    db.commit()
    log.info("dedupe_images: deleted %d rows", deleted)
    return deleted


def drop_raw_html(db: sqlite3.Connection) -> int:
    log.info("drop_raw_html: NULLing non-wiki raw_html...")
    cur = db.execute(
        "UPDATE pages SET raw_html = NULL "
        "WHERE raw_html IS NOT NULL AND domain != 'en.wikipedia.org'"
    )
    affected = cur.rowcount
    db.commit()
    log.info("drop_raw_html: cleared %d rows", affected)
    return affected


def add_unique_indexes(db: sqlite3.Connection) -> None:
    log.info("add_unique_indexes...")
    for stmt in (
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_links_source_target "
        "ON links(source_id, target_url)",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_page_images_page_src "
        "ON page_images(page_id, src_url)",
        "CREATE INDEX IF NOT EXISTS idx_pages_content_hash "
        "ON pages(content_hash)",
    ):
        try:
            db.execute(stmt)
            db.commit()
        except sqlite3.OperationalError as exc:
            log.warning("  skipped (%s): %s", stmt.split()[5], exc)


def add_helper_indexes(db: sqlite3.Connection) -> None:
    """Create the indexes that make the rewire loop fast.

    Without these, every UPDATE/DELETE WHERE source_id=? or page_id=?
    is a full-table scan over millions of rows.
    """
    log.info("add_helper_indexes...")
    for stmt in (
        "CREATE INDEX IF NOT EXISTS idx_links_source_id ON links(source_id)",
        "CREATE INDEX IF NOT EXISTS idx_postings_page_id ON postings(page_id)",
    ):
        t0 = time.time()
        log.info("  %s", stmt)
        db.execute(stmt)
        db.commit()
        log.info("    done in %.1fs", time.time() - t0)


def vacuum(db: sqlite3.Connection) -> None:
    log.info("VACUUM (this may take a while)...")
    t0 = time.time()
    db.execute("VACUUM")
    db.commit()
    log.info("VACUUM done in %.1fs", time.time() - t0)


def main(db_path: Path, dry_run: bool) -> int:
    if not db_path.exists():
        log.error("DB not found: %s", db_path)
        return 1

    # Use /temp for sqlite tempfiles to avoid filling /home.
    tmpdir = "/temp" if Path("/temp").exists() and os.access("/temp", os.W_OK) else None
    if tmpdir:
        os.environ["SQLITE_TMPDIR"] = tmpdir
        os.environ["TMPDIR"] = tmpdir
        log.info("Using tempdir: %s", tmpdir)

    db = sqlite3.connect(str(db_path), timeout=120)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=OFF")
    db.execute("PRAGMA synchronous=NORMAL")
    db.execute("PRAGMA temp_store=FILE")
    if tmpdir:
        db.execute(f"PRAGMA temp_store_directory='{tmpdir}'")

    try:
        report(db, db_path, "before")
        if dry_run:
            log.info("--dry-run: stopping before mutations")
            return 0

        add_helper_indexes(db)
        dedupe_pages(db)
        dedupe_links(db)
        dedupe_images(db)
        drop_raw_html(db)
        add_unique_indexes(db)

        log.info("==== Pre-VACUUM stats ====")
        report(db, db_path, "pre-vacuum")

        # Close/reopen with isolation_level=None so VACUUM works (it cannot run
        # inside a transaction). sqlite3 module's default autocommit handling
        # plus an explicit COMMIT should be fine here, but be defensive:
        db.isolation_level = None
        vacuum(db)

        report(db, db_path, "after")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("db_path", type=Path)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    sys.exit(main(args.db_path, args.dry_run))

"""One-shot DB maintenance: dedupe content, drop raw_html (non-wiki), compact.

Run on the live App Service container:

    python scripts/dedupe_and_compact.py            # full run
    python scripts/dedupe_and_compact.py --dry-run  # just report

Order of operations:
    1.  Report current sizes / duplicate counts.
    2.  Delete duplicate `pages` rows with the same content_hash
        (keep the lowest id, redirect dependent rows).
    3.  Delete duplicate `links` rows (same source_id + target_url).
    4.  Delete duplicate `page_images` rows (same page_id + src_url).
    5.  NULL out `pages.raw_html` for everything except en.wikipedia.org.
    6.  Enable incremental_vacuum and reclaim freed pages.
    7.  Print the final size.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

# Allow running as a plain script (no package install needed in container).
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import aiosqlite  # noqa: E402

from platysearch.config import get_settings  # noqa: E402

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
)
log = logging.getLogger("dedupe")


async def _scalar(db: aiosqlite.Connection, sql: str) -> int:
    cur = await db.execute(sql)
    row = await cur.fetchone()
    await cur.close()
    return int(row[0]) if row and row[0] is not None else 0


async def _file_size_mb(path: Path) -> float:
    if not path.exists():
        return 0.0
    return path.stat().st_size / (1024 * 1024)


async def report(db: aiosqlite.Connection, db_path: Path) -> None:
    pages = await _scalar(db, "SELECT COUNT(*) FROM pages")
    pages_with_hash = await _scalar(
        db, "SELECT COUNT(*) FROM pages WHERE content_hash IS NOT NULL"
    )
    distinct_hashes = await _scalar(
        db,
        "SELECT COUNT(DISTINCT content_hash) FROM pages "
        "WHERE content_hash IS NOT NULL",
    )
    dup_pages = pages_with_hash - distinct_hashes
    raw_html_rows = await _scalar(
        db, "SELECT COUNT(*) FROM pages WHERE raw_html IS NOT NULL"
    )
    raw_html_nonwiki = await _scalar(
        db,
        "SELECT COUNT(*) FROM pages WHERE raw_html IS NOT NULL "
        "AND domain != 'en.wikipedia.org'",
    )
    links = await _scalar(db, "SELECT COUNT(*) FROM links")
    distinct_links = await _scalar(
        db, "SELECT COUNT(*) FROM (SELECT 1 FROM links GROUP BY source_id, target_url)"
    )
    dup_links = links - distinct_links
    images = await _scalar(db, "SELECT COUNT(*) FROM page_images")
    distinct_images = await _scalar(
        db,
        "SELECT COUNT(*) FROM (SELECT 1 FROM page_images GROUP BY page_id, src_url)",
    )
    dup_images = images - distinct_images
    postings = await _scalar(db, "SELECT COUNT(*) FROM postings")
    queue = await _scalar(db, "SELECT COUNT(*) FROM crawl_queue")

    size = await _file_size_mb(db_path)

    log.info("---- DB report ----")
    log.info("file size:                 %.1f MB", size)
    log.info("pages:                     %d", pages)
    log.info("  with content_hash:       %d", pages_with_hash)
    log.info("  distinct hashes:         %d", distinct_hashes)
    log.info("  duplicate hash rows:     %d", dup_pages)
    log.info("  rows with raw_html:      %d", raw_html_rows)
    log.info("  raw_html non-wiki rows:  %d  (will be NULLed)", raw_html_nonwiki)
    log.info("links:                     %d  (duplicates: %d)", links, dup_links)
    log.info("page_images:               %d  (duplicates: %d)", images, dup_images)
    log.info("postings:                  %d", postings)
    log.info("crawl_queue:               %d", queue)


async def dedupe_pages(db: aiosqlite.Connection) -> int:
    """Delete pages with duplicate content_hash, keeping the smallest id.

    Also rewires `links.source_id`, `postings.page_id`, `page_scores.page_id`,
    and `page_images.page_id` to point at the surviving page.
    """
    # Build mapping of dup_id -> keep_id for content-hash collisions.
    cur = await db.execute(
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
    )
    pairs = await cur.fetchall()
    await cur.close()

    if not pairs:
        log.info("No duplicate-hash pages.")
        return 0

    log.info("Rewiring %d duplicate pages...", len(pairs))
    # Rewire dependents. Use INSERT OR IGNORE-style updates to avoid PK clashes.
    for dup_id, keep_id in pairs:
        await db.execute(
            "UPDATE OR IGNORE links SET source_id = ? WHERE source_id = ?",
            (keep_id, dup_id),
        )
        await db.execute("DELETE FROM links WHERE source_id = ?", (dup_id,))

        await db.execute(
            "UPDATE OR IGNORE postings SET page_id = ? WHERE page_id = ?",
            (keep_id, dup_id),
        )
        await db.execute("DELETE FROM postings WHERE page_id = ?", (dup_id,))

        await db.execute(
            "UPDATE OR IGNORE page_images SET page_id = ? WHERE page_id = ?",
            (keep_id, dup_id),
        )
        await db.execute("DELETE FROM page_images WHERE page_id = ?", (dup_id,))

        await db.execute("DELETE FROM page_scores WHERE page_id = ?", (dup_id,))

    # Finally drop the duplicate page rows.
    ids = [d for d, _ in pairs]
    placeholders = ",".join("?" * len(ids))
    await db.execute(f"DELETE FROM pages WHERE id IN ({placeholders})", ids)  # noqa: S608
    await db.commit()
    log.info("Deleted %d duplicate pages.", len(ids))
    return len(ids)


async def dedupe_links(db: aiosqlite.Connection) -> int:
    cur = await db.execute(
        "SELECT COUNT(*) - COUNT(*) "  # noqa: ISC003
        "FROM (SELECT 1 FROM links GROUP BY source_id, target_url)"
    )
    await cur.close()
    log.info("Compacting links table...")
    # Rebuild keeping only the lowest id per (source_id, target_url).
    await db.execute(
        """
        DELETE FROM links
        WHERE id NOT IN (
            SELECT MIN(id) FROM links GROUP BY source_id, target_url
        )
        """
    )
    deleted = db.total_changes
    await db.commit()
    log.info("links: deleted %d duplicate rows", deleted)
    return deleted


async def dedupe_images(db: aiosqlite.Connection) -> int:
    log.info("Compacting page_images table...")
    await db.execute(
        """
        DELETE FROM page_images
        WHERE id NOT IN (
            SELECT MIN(id) FROM page_images GROUP BY page_id, src_url
        )
        """
    )
    deleted = db.total_changes
    await db.commit()
    log.info("page_images: deleted %d duplicate rows", deleted)
    return deleted


async def drop_raw_html(db: aiosqlite.Connection) -> int:
    log.info("NULLing raw_html for non-Wikipedia pages...")
    await db.execute(
        "UPDATE pages SET raw_html = NULL "
        "WHERE raw_html IS NOT NULL AND domain != 'en.wikipedia.org'"
    )
    affected = db.total_changes
    await db.commit()
    log.info("raw_html: cleared from %d rows", affected)
    return affected


async def add_unique_indexes(db: aiosqlite.Connection) -> None:
    log.info("Creating UNIQUE indexes...")
    for stmt in (
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_links_source_target "
        "ON links(source_id, target_url)",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_page_images_page_src "
        "ON page_images(page_id, src_url)",
    ):
        try:
            await db.execute(stmt)
            await db.commit()
        except Exception as exc:  # noqa: BLE001
            log.warning("Skipping unique index: %s", exc)


async def compact(db: aiosqlite.Connection) -> None:
    log.info("Enabling incremental_vacuum + reclaiming free pages...")
    # auto_vacuum can only be set on a fresh DB, but incremental_vacuum
    # also works on databases already created with auto_vacuum=NONE if we
    # first run a full VACUUM. Try incremental first; fall back to VACUUM.
    try:
        await db.execute("PRAGMA incremental_vacuum")
        await db.commit()
        log.info("incremental_vacuum complete.")
    except Exception as exc:  # noqa: BLE001
        log.warning("incremental_vacuum failed (%s) — running full VACUUM.", exc)
        await db.execute("VACUUM")
        await db.commit()
        log.info("VACUUM complete.")


async def main(dry_run: bool) -> None:
    settings = get_settings()
    db_path = settings.db_path
    log.info("DB path: %s", db_path)

    db = await aiosqlite.connect(str(db_path))
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA foreign_keys=OFF")  # we manage rewiring manually
    await db.execute("PRAGMA synchronous=NORMAL")
    await db.execute("PRAGMA temp_store=MEMORY")

    try:
        await report(db, db_path)
        if dry_run:
            log.info("--dry-run: stopping before any modifications.")
            return

        await dedupe_pages(db)
        await dedupe_links(db)
        await dedupe_images(db)
        await drop_raw_html(db)
        await add_unique_indexes(db)
        await compact(db)

        log.info("---- After ----")
        await report(db, db_path)
    finally:
        await db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="report only")
    args = parser.parse_args()
    asyncio.run(main(args.dry_run))

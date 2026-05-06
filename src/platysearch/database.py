"""SQLite database initialisation and access helpers."""

from __future__ import annotations

import logging

import aiosqlite
from datetime import datetime
from pathlib import Path

from platysearch.config import get_settings

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS pages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    url         TEXT    UNIQUE NOT NULL,
    domain      TEXT    NOT NULL,
    title       TEXT,
    body        TEXT,
    raw_html    TEXT,
    fetched_at  TEXT,
    status_code INTEGER,
    content_hash TEXT,
    content_type TEXT DEFAULT 'page'
);

CREATE TABLE IF NOT EXISTS links (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL REFERENCES pages(id),
    target_url TEXT   NOT NULL
);

CREATE TABLE IF NOT EXISTS terms (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    term TEXT    UNIQUE NOT NULL
);

CREATE TABLE IF NOT EXISTS postings (
    term_id   INTEGER NOT NULL REFERENCES terms(id),
    page_id   INTEGER NOT NULL REFERENCES pages(id),
    tf        REAL    NOT NULL,
    positions TEXT,
    PRIMARY KEY (term_id, page_id)
);

CREATE TABLE IF NOT EXISTS page_scores (
    page_id       INTEGER PRIMARY KEY REFERENCES pages(id),
    inbound_links INTEGER DEFAULT 0,
    domain_diversity INTEGER DEFAULT 0,
    content_length INTEGER DEFAULT 0,
    ai_score      REAL DEFAULT 0.0,
    quality_score REAL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS crawl_queue (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    url      TEXT    UNIQUE NOT NULL,
    domain   TEXT    NOT NULL,
    depth    INTEGER DEFAULT 0,
    added_at TEXT    DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS page_images (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    page_id  INTEGER NOT NULL REFERENCES pages(id),
    src_url  TEXT    NOT NULL,
    alt_text TEXT    DEFAULT '',
    width    INTEGER DEFAULT 0,
    height   INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_pages_url ON pages(url);
CREATE INDEX IF NOT EXISTS idx_pages_domain ON pages(domain);
CREATE INDEX IF NOT EXISTS idx_terms_term ON terms(term);
CREATE INDEX IF NOT EXISTS idx_postings_term ON postings(term_id);
CREATE INDEX IF NOT EXISTS idx_crawl_queue_domain ON crawl_queue(domain);
CREATE INDEX IF NOT EXISTS idx_links_target ON links(target_url);
CREATE INDEX IF NOT EXISTS idx_page_images_page ON page_images(page_id);

CREATE TABLE IF NOT EXISTS custom_seeds (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    url      TEXT    UNIQUE NOT NULL,
    category TEXT    DEFAULT 'general',
    added_at TEXT    DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS job_history (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    job_type       TEXT    NOT NULL,
    started_at     TEXT    NOT NULL,
    finished_at    TEXT,
    status         TEXT    NOT NULL DEFAULT 'running',
    pages_crawled  INTEGER DEFAULT 0,
    pages_indexed  INTEGER DEFAULT 0,
    pages_scored   INTEGER DEFAULT 0,
    error          TEXT    DEFAULT ''
);
"""


async def get_db() -> aiosqlite.Connection:
    """Return a connection to the application database."""
    settings = get_settings()
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    db = await aiosqlite.connect(str(settings.db_path))
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA foreign_keys=ON")
    # SQLite page cache: ~64 MB (negative = KB). Tuned for B2 (3.5 GB).
    await db.execute("PRAGMA cache_size=-65536")
    # Disable memory-mapped I/O to keep RSS predictable.
    await db.execute("PRAGMA mmap_size=0")
    # Faster writes \u2014 still safe with WAL.
    await db.execute("PRAGMA synchronous=NORMAL")
    await db.execute("PRAGMA temp_store=MEMORY")
    return db


async def init_db() -> None:
    """Create all tables if they don't exist."""
    db = await get_db()
    try:
        await db.executescript(_SCHEMA)
        await db.commit()
    finally:
        await db.close()


# ── Meta key/value helpers ───────────────────────────────────────────────────


async def get_meta(key: str) -> str | None:
    db = await get_db()
    try:
        cur = await db.execute("SELECT value FROM meta WHERE key = ?", (key,))
        row = await cur.fetchone()
        return row[0] if row else None
    finally:
        await db.close()


async def set_meta(key: str, value: str) -> None:
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        await db.commit()
    finally:
        await db.close()


# ── Custom seed helpers ──────────────────────────────────────────────────────


async def list_custom_seeds() -> list[dict]:
    db = await get_db()
    try:
        rows = await db.execute_fetchall(
            "SELECT id, url, category, added_at FROM custom_seeds ORDER BY added_at DESC"
        )
        return [dict(r) for r in rows]
    finally:
        await db.close()


async def add_custom_seed(url: str, category: str = "general") -> bool:
    db = await get_db()
    try:
        await db.execute(
            "INSERT OR IGNORE INTO custom_seeds (url, category) VALUES (?, ?)",
            (url, category),
        )
        await db.commit()
        return db.total_changes > 0
    finally:
        await db.close()


async def remove_custom_seed(seed_id: int) -> None:
    db = await get_db()
    try:
        await db.execute("DELETE FROM custom_seeds WHERE id = ?", (seed_id,))
        await db.commit()
    finally:
        await db.close()


async def get_custom_seed_urls() -> list[str]:
    db = await get_db()
    try:
        rows = await db.execute_fetchall("SELECT url FROM custom_seeds")
        return [r[0] for r in rows]
    finally:
        await db.close()


# ── Job history helpers ──────────────────────────────────────────────────────


async def save_job(job_type: str, started_at: str, finished_at: str | None,
                   status: str, pages_crawled: int, pages_indexed: int,
                   pages_scored: int, error: str) -> int:
    """Insert a job record and return its row id."""
    db = await get_db()
    try:
        cur = await db.execute(
            "INSERT INTO job_history (job_type, started_at, finished_at, status, "
            "pages_crawled, pages_indexed, pages_scored, error) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (job_type, started_at, finished_at, status, pages_crawled,
             pages_indexed, pages_scored, error),
        )
        await db.commit()
        return cur.lastrowid  # type: ignore[return-value]
    finally:
        await db.close()


async def update_job(row_id: int, *, finished_at: str | None = None,
                     status: str | None = None, pages_crawled: int | None = None,
                     pages_indexed: int | None = None, pages_scored: int | None = None,
                     error: str | None = None) -> None:
    """Update selected fields of an existing job record."""
    parts: list[str] = []
    vals: list[object] = []
    if finished_at is not None:
        parts.append("finished_at = ?")
        vals.append(finished_at)
    if status is not None:
        parts.append("status = ?")
        vals.append(status)
    if pages_crawled is not None:
        parts.append("pages_crawled = ?")
        vals.append(pages_crawled)
    if pages_indexed is not None:
        parts.append("pages_indexed = ?")
        vals.append(pages_indexed)
    if pages_scored is not None:
        parts.append("pages_scored = ?")
        vals.append(pages_scored)
    if error is not None:
        parts.append("error = ?")
        vals.append(error)
    if not parts:
        return
    vals.append(row_id)
    db = await get_db()
    try:
        await db.execute(
            f"UPDATE job_history SET {', '.join(parts)} WHERE id = ?",  # noqa: S608
            tuple(vals),
        )
        await db.commit()
    finally:
        await db.close()


async def mark_stale_jobs_crashed() -> int:
    """Mark any 'running' jobs as 'crashed' — called on startup after a restart."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "UPDATE job_history SET status = 'crashed', "
            "finished_at = datetime('now') "
            "WHERE status = 'running'",
        )
        await db.commit()
        count = cursor.rowcount
        if count:
            log.info("Marked %d stale running job(s) as crashed.", count)
        return count
    except Exception:
        log.exception("Failed to mark stale jobs as crashed.")
        return 0


async def load_job_history(limit: int = 20) -> list[dict]:
    """Return the most recent job records, newest first."""
    db = await get_db()
    try:
        rows = await db.execute_fetchall(
            "SELECT id, job_type, started_at, finished_at, status, "
            "pages_crawled, pages_indexed, pages_scored, error "
            "FROM job_history ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        result = []
        for r in rows:
            d = dict(r)
            # Compute human-readable duration string.
            if d.get("finished_at") and d.get("started_at"):
                try:
                    t0 = datetime.fromisoformat(d["started_at"])
                    t1 = datetime.fromisoformat(d["finished_at"])
                    secs = int((t1 - t0).total_seconds())
                    d["duration"] = f"{secs // 60}m {secs % 60}s"
                except (ValueError, TypeError):
                    d["duration"] = ""
            else:
                d["duration"] = ""
            result.append(d)
        return result
    finally:
        await db.close()

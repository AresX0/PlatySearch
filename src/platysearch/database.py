"""SQLite database initialisation and access helpers."""

from __future__ import annotations

import aiosqlite
from pathlib import Path

from platysearch.config import get_settings

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
"""


async def get_db() -> aiosqlite.Connection:
    """Return a connection to the application database."""
    settings = get_settings()
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    db = await aiosqlite.connect(str(settings.db_path))
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA foreign_keys=ON")
    return db


async def init_db() -> None:
    """Create all tables if they don't exist."""
    db = await get_db()
    try:
        await db.executescript(_SCHEMA)
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

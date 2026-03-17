"""Async web crawler — discovers and fetches pages from seed URLs."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from urllib.parse import urlparse

import aiohttp

from platysearch.classifier import classify_page
from platysearch.config import get_settings
from platysearch.database import get_db
from platysearch.parser import parse_html
from platysearch.robots import RobotsChecker

log = logging.getLogger(__name__)


async def crawl(seeds: list[str], max_pages: int | None = None) -> int:
    """Crawl starting from *seeds*. Returns the number of pages fetched."""
    settings = get_settings()
    max_pages = max_pages or settings.max_pages

    db = await get_db()
    try:
        # Seed the queue.
        for url in seeds:
            domain = urlparse(url).netloc
            await db.execute(
                "INSERT OR IGNORE INTO crawl_queue (url, domain, depth) VALUES (?, ?, 0)",
                (url, domain),
            )
        await db.commit()

        fetched = 0
        robots = RobotsChecker(settings.user_agent)

        connector = aiohttp.TCPConnector(limit=10, limit_per_host=2)
        timeout = aiohttp.ClientTimeout(total=30)
        headers = {"User-Agent": settings.user_agent}

        async with aiohttp.ClientSession(
            connector=connector, timeout=timeout, headers=headers
        ) as session:
            while fetched < max_pages:
                # Pick next URL from queue.
                row = await db.execute_fetchall(
                    "SELECT id, url, domain, depth FROM crawl_queue ORDER BY depth ASC LIMIT 1"
                )
                if not row:
                    log.info("Crawl queue empty — stopping.")
                    break
                queue_id, url, domain, depth = row[0]

                # Remove from queue.
                await db.execute("DELETE FROM crawl_queue WHERE id = ?", (queue_id,))
                await db.commit()

                # Skip if already fetched.
                exists = await db.execute_fetchall(
                    "SELECT 1 FROM pages WHERE url = ?", (url,)
                )
                if exists:
                    continue

                # Robots check.
                if not await robots.is_allowed(url, session):
                    log.debug("Blocked by robots.txt: %s", url)
                    continue

                # Rate limit.
                await robots.wait_for_rate_limit(domain, settings.crawl_delay)

                # Fetch.
                try:
                    page_data = await _fetch_page(session, url)
                except Exception as exc:
                    log.warning("Failed to fetch %s: %s", url, exc)
                    continue

                if page_data is None:
                    continue

                status_code, html = page_data

                # Parse.
                parsed = parse_html(url, html)

                # Classify content type.
                content_type = classify_page(url, parsed.title, html)

                # Store page.
                await db.execute(
                    """INSERT OR IGNORE INTO pages
                       (url, domain, title, body, raw_html, fetched_at, status_code, content_hash, content_type)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        url,
                        domain,
                        parsed.title,
                        parsed.body,
                        html,
                        datetime.now(timezone.utc).isoformat(),
                        status_code,
                        parsed.content_hash,
                        content_type,
                    ),
                )

                # Get page id.
                cursor = await db.execute("SELECT id FROM pages WHERE url = ?", (url,))
                page_row = await cursor.fetchone()
                page_id = page_row[0]

                # Store outbound links & enqueue new ones.
                for link in parsed.links:
                    await db.execute(
                        "INSERT INTO links (source_id, target_url) VALUES (?, ?)",
                        (page_id, link),
                    )
                    link_domain = urlparse(link).netloc
                    await db.execute(
                        "INSERT OR IGNORE INTO crawl_queue (url, domain, depth) VALUES (?, ?, ?)",
                        (link, link_domain, depth + 1),
                    )

                await db.commit()
                fetched += 1
                log.info("[%d] Crawled: %s (%s)", fetched, parsed.title or "(no title)", url)

        return fetched
    finally:
        await db.close()


async def _fetch_page(
    session: aiohttp.ClientSession, url: str
) -> tuple[int, str] | None:
    """Fetch a single page, returning (status_code, html) or None."""
    async with session.get(url, allow_redirects=True, max_redirects=5) as resp:
        content_type = resp.headers.get("Content-Type", "")
        if "text/html" not in content_type:
            return None
        # Limit body to 2 MB to avoid memory issues.
        body = await resp.read()
        if len(body) > 2 * 1024 * 1024:
            return None
        html = body.decode(resp.get_encoding() or "utf-8", errors="replace")
        return resp.status, html

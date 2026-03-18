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
    concurrency = settings.crawl_concurrency

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
        in_flight: set[str] = set()  # URLs currently being fetched
        robots = RobotsChecker(settings.user_agent)

        connector = aiohttp.TCPConnector(limit=concurrency * 4, limit_per_host=3)
        timeout = aiohttp.ClientTimeout(total=30)
        headers = {"User-Agent": settings.user_agent}

        async with aiohttp.ClientSession(
            connector=connector, timeout=timeout, headers=headers
        ) as session:

            async def _process_one(url: str, domain: str, depth: int) -> bool:
                """Fetch, parse, and store a single page. Returns True if stored."""
                if not await robots.is_allowed(url, session):
                    log.debug("Blocked by robots.txt: %s", url)
                    return False

                await robots.wait_for_rate_limit(domain, settings.crawl_delay)

                try:
                    page_data = await _fetch_page(session, url)
                except Exception as exc:
                    log.warning("Failed to fetch %s: %s", url, exc)
                    return False

                if page_data is None:
                    return False

                status_code, html = page_data
                parsed = parse_html(url, html)
                content_type = classify_page(url, parsed.title, html)

                return (url, domain, parsed, html, status_code, content_type, depth)

            while fetched < max_pages:
                # Grab a batch of URLs from the queue.
                batch_size = min(concurrency, max_pages - fetched)
                rows = await db.execute_fetchall(
                    "SELECT id, url, domain, depth FROM crawl_queue ORDER BY depth ASC LIMIT ?",
                    (batch_size * 3,),  # over-fetch to account for skips
                )
                if not rows:
                    log.info("Crawl queue empty — stopping.")
                    break

                # Filter batch: remove already-fetched and in-flight URLs.
                batch = []
                for queue_id, url, domain, depth in rows:
                    await db.execute("DELETE FROM crawl_queue WHERE id = ?", (queue_id,))
                    if url in in_flight:
                        continue
                    exists = await db.execute_fetchall(
                        "SELECT 1 FROM pages WHERE url = ?", (url,)
                    )
                    if exists:
                        continue
                    batch.append((url, domain, depth))
                    in_flight.add(url)
                    if len(batch) >= batch_size:
                        break
                await db.commit()

                if not batch:
                    continue

                # Fetch all URLs in the batch concurrently.
                tasks = [
                    asyncio.create_task(_process_one(url, domain, depth))
                    for url, domain, depth in batch
                ]
                results = await asyncio.gather(*tasks, return_exceptions=True)

                for (url, domain, depth), result in zip(batch, results):
                    in_flight.discard(url)
                    if isinstance(result, Exception):
                        log.warning("Worker error for %s: %s", url, result)
                        continue
                    if result is False:
                        continue

                    _, _, parsed, html, status_code, content_type, depth = result

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

                    cursor = await db.execute("SELECT id FROM pages WHERE url = ?", (url,))
                    page_row = await cursor.fetchone()
                    page_id = page_row[0]

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

                    fetched += 1
                    log.info("[%d] Crawled: %s (%s)", fetched, parsed.title or "(no title)", url)

                await db.commit()

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

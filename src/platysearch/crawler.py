"""Async web crawler — discovers and fetches pages from seed URLs."""

from __future__ import annotations

import asyncio
import logging
import time
import typing
from datetime import datetime, timezone
from urllib.parse import urlparse

import aiohttp

from platysearch.classifier import classify_page
from platysearch.config import get_settings
from platysearch.database import get_db
from platysearch.parser import parse_html
from platysearch.robots import RobotsChecker

log = logging.getLogger(__name__)


async def crawl(
    seeds: list[str],
    max_pages: int | None = None,
    skip_domains: set[str] | None = None,
    incremental_index_interval: int = 0,
    max_seconds: int = 0,
    cancel_check: typing.Callable[[], bool] | None = None,
) -> int:
    """Crawl starting from *seeds*. Returns the number of pages fetched.

    *skip_domains* — if given, URLs on these domains are left in the queue
    but not fetched this run (they stay queued for a later crawl).
    *incremental_index_interval* — if >0, re-index every N pages.
    *max_seconds* — if >0, stop crawling after this many seconds.
    *cancel_check* — if provided, called each loop iteration; return True to stop.
    """
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
        domain_failures: dict[str, int] = {}  # track failures per domain
        domain_fetched: dict[str, int] = {}  # pages stored per domain this run
        # Cap per domain so one site doesn't consume the whole budget.
        # Scales with budget: ~5% of max_pages, min 50, max 500.
        per_domain_cap = max(50, min(500, max_pages // 20))
        crawl_start = time.monotonic()
        robots = RobotsChecker(settings.user_agent)

        import ssl as _ssl
        ssl_ctx = _ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = _ssl.CERT_NONE
        connector = aiohttp.TCPConnector(
            limit=concurrency * 4, limit_per_host=3, ssl=ssl_ctx,
        )
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
                # Check cancellation.
                if cancel_check is not None and cancel_check():
                    log.info("Crawl cancelled by admin after %d pages.", fetched)
                    break

                # Check time budget.
                if max_seconds > 0 and (time.monotonic() - crawl_start) >= max_seconds:
                    log.info("Crawl time limit reached (%ds) after %d pages — stopping.", max_seconds, fetched)
                    break

                # Grab a batch of URLs from the queue.
                # ORDER BY depth ASC, RANDOM() spreads work across domains at
                # the same depth instead of draining one domain at a time.
                batch_size = min(concurrency, max_pages - fetched)
                rows = await db.execute_fetchall(
                    "SELECT id, url, domain, depth FROM crawl_queue "
                    "ORDER BY depth ASC, RANDOM() LIMIT ?",
                    (batch_size * 20,),  # over-fetch aggressively to handle dead URLs
                )
                if not rows:
                    log.info("Crawl queue empty — stopping.")
                    break

                # Filter batch: remove already-fetched and in-flight URLs.
                batch = []
                skipped_rows = []
                capped_count = 0  # urls left for next run because their domain hit the cap
                considered = 0
                for queue_id, url, domain, depth in rows:
                    considered += 1
                    # If domain is in the skip list, leave it in the queue.
                    if skip_domains and domain in skip_domains:
                        skipped_rows.append((queue_id, url, domain, depth))
                        continue
                    # Skip domains that have failed too many times (likely dead).
                    if domain_failures.get(domain, 0) >= 5:
                        await db.execute("DELETE FROM crawl_queue WHERE id = ?", (queue_id,))
                        continue
                    # Per-run cap: if this domain has already filled its slice,
                    # leave the URL in the queue for a later run and move on.
                    if domain_fetched.get(domain, 0) >= per_domain_cap:
                        capped_count += 1
                        continue
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
                # Delete skipped rows so they don't clog the top of the queue;
                # re-insert them at a higher depth so non-skipped URLs are prioritised.
                for queue_id, url, domain, depth in skipped_rows:
                    await db.execute("DELETE FROM crawl_queue WHERE id = ?", (queue_id,))
                    await db.execute(
                        "INSERT OR IGNORE INTO crawl_queue (url, domain, depth) VALUES (?, ?, ?)",
                        (url, domain, depth + 1000),
                    )
                await db.commit()

                if not batch:
                    # If everything we pulled was capped (per-domain limit) and
                    # there were no other usable rows, we'd loop forever. Stop.
                    if capped_count > 0 and capped_count == considered:
                        log.info(
                            "All queued URLs are for capped domains \u2014 ending crawl run "
                            "(fetched=%d, per_domain_cap=%d).",
                            fetched, per_domain_cap,
                        )
                        break
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
                        domain_failures[domain] = domain_failures.get(domain, 0) + 1
                        continue
                    if result is False:
                        domain_failures[domain] = domain_failures.get(domain, 0) + 1
                        continue

                    _, _, parsed, html, status_code, content_type, depth = result

                    # Only persist raw HTML for Wikipedia (used by refextractor);
                    # for everything else we keep just the parsed body to save space.
                    stored_html = html if domain == "en.wikipedia.org" else None

                    await db.execute(
                        """INSERT OR IGNORE INTO pages
                           (url, domain, title, body, raw_html, fetched_at, status_code, content_hash, content_type)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            url,
                            domain,
                            parsed.title,
                            parsed.body,
                            stored_html,
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
                            "INSERT OR IGNORE INTO links (source_id, target_url) VALUES (?, ?)",
                            (page_id, link),
                        )
                        link_domain = urlparse(link).netloc
                        await db.execute(
                            "INSERT OR IGNORE INTO crawl_queue (url, domain, depth) VALUES (?, ?, ?)",
                            (link, link_domain, depth + 1),
                        )

                    # Store extracted images.
                    for img in parsed.images:
                        await db.execute(
                            "INSERT OR IGNORE INTO page_images (page_id, src_url, alt_text, width, height) "
                            "VALUES (?, ?, ?, ?, ?)",
                            (page_id, img.src, img.alt, img.width, img.height),
                        )

                    fetched += 1
                    domain_fetched[domain] = domain_fetched.get(domain, 0) + 1
                    log.info("[%d] Crawled: %s (%s)", fetched, parsed.title or "(no title)", url)

                await db.commit()

                # Incremental indexing — re-index periodically during crawl.
                if (
                    incremental_index_interval > 0
                    and fetched % incremental_index_interval == 0
                ):
                    log.info("Incremental index at %d pages...", fetched)
                    await db.close()
                    from platysearch.indexer import index_all_pages
                    await index_all_pages()
                    db = await get_db()

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

"""Extract external reference URLs from crawled Wikipedia pages and crawl them."""

from __future__ import annotations

import logging
import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from platysearch.crawler import crawl
from platysearch.database import get_db

log = logging.getLogger(__name__)

# Domains to skip — internal Wikipedia / Wikimedia links, archive snapshots, etc.
_SKIP_DOMAINS = {
    "en.wikipedia.org",
    "en.m.wikipedia.org",
    "commons.wikimedia.org",
    "upload.wikimedia.org",
    "web.archive.org",
    "tools.wmflabs.org",
    "www.wikidata.org",
    "wikidata.org",
}

# Only accept http(s) links that look like real external content.
_GOOD_SCHEMES = {"http", "https"}


async def _collect_ref_urls() -> list[str]:
    """Scan all stored Wikipedia pages and return external reference URLs."""
    db = await get_db()
    try:
        rows = await db.execute_fetchall(
            "SELECT url, raw_html FROM pages WHERE domain = 'en.wikipedia.org'"
        )
    finally:
        await db.close()

    seen: set[str] = set()
    ref_urls: list[str] = []

    for page_url, raw_html in rows:
        if not raw_html:
            continue
        soup = BeautifulSoup(raw_html, "lxml")

        # Wikipedia references live inside <ol class="references"> and
        # <div class="reflist"> sections, plus "External links" sections.
        ref_containers = soup.select("ol.references, div.reflist, div.refbegin")
        ext_links_heading = soup.find("span", id="External_links")
        if ext_links_heading:
            # Grab the next sibling <ul> or <div> after the heading
            parent = ext_links_heading.find_parent(re.compile(r"^h[2-4]$"))
            if parent:
                for sib in parent.find_next_siblings():
                    if sib.name and re.match(r"^h[2-4]$", sib.name):
                        break
                    ref_containers.append(sib)

        for container in ref_containers:
            for a_tag in container.find_all("a", href=True):
                href = a_tag["href"]
                parsed = urlparse(href)
                if parsed.scheme not in _GOOD_SCHEMES:
                    continue
                domain = parsed.netloc.lower()
                if domain in _SKIP_DOMAINS:
                    continue
                # Skip fragment-only or very short URLs
                if len(href) < 15:
                    continue
                if href not in seen:
                    seen.add(href)
                    ref_urls.append(href)

    log.info("Extracted %d unique external reference URLs from %d Wikipedia pages", len(ref_urls), len(rows))
    return ref_urls


async def extract_and_crawl_refs(max_pages: int = 500) -> int:
    """Extract reference URLs from Wikipedia pages and crawl them."""
    ref_urls = await _collect_ref_urls()
    if not ref_urls:
        log.info("No reference URLs found.")
        return 0

    log.info("Seeding crawler with %d reference URLs (max %d pages)", len(ref_urls), max_pages)
    return await crawl(ref_urls, max_pages=max_pages)

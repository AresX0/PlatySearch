"""Inject external links from Wikipedia pages into crawl queue at high priority."""
from __future__ import annotations

import sqlite3
from urllib.parse import urlparse

DB = "data/platysearch.db"

WIKI_DOMAINS = {
    "wikipedia.org", "wikimedia.org", "wikidata.org",
    "wikisource.org", "wiktionary.org", "mediawiki.org",
}
SKIP_DOMAINS = {
    "web.archive.org", "archive.org", "doi.org",
    "books.google.com", "books.google.fr", "books.google.de",
    # Low-value link farms
    "sf-encyclopedia.com",  # 335k links, likely auto-generated
}

def main() -> None:
    c = sqlite3.connect(DB)

    # Get external links from wiki pages
    rows = c.execute("""
        SELECT DISTINCT l.target_url FROM links l
        JOIN pages p ON l.source_id = p.id
        WHERE p.url LIKE '%wikipedia.org%'
        AND l.target_url LIKE 'http%'
    """).fetchall()
    print(f"Total external links from wiki pages: {len(rows)}")

    # Filter and inject
    injected = 0
    skipped = 0
    for (url,) in rows:
        domain = urlparse(url).netloc.lower()
        if not domain:
            continue
        # Skip wiki and low-value domains
        if any(wd in domain for wd in WIKI_DOMAINS):
            continue
        if domain in SKIP_DOMAINS:
            continue
        # Check if already in queue or pages
        exists_q = c.execute("SELECT 1 FROM crawl_queue WHERE url = ?", (url,)).fetchone()
        exists_p = c.execute("SELECT 1 FROM pages WHERE url = ?", (url,)).fetchone()
        if exists_q or exists_p:
            skipped += 1
            continue
        c.execute(
            "INSERT OR IGNORE INTO crawl_queue (url, domain, depth) VALUES (?, ?, 0)",
            (url, domain, ),
        )
        injected += 1
        if injected % 5000 == 0:
            c.commit()
            print(f"  Injected {injected}...")
    c.commit()
    print(f"Injected: {injected}, Already queued/crawled: {skipped}")

    # Stats
    total_q = c.execute("SELECT COUNT(*) FROM crawl_queue").fetchone()[0]
    nonwiki = c.execute("""SELECT COUNT(*) FROM crawl_queue 
        WHERE url NOT LIKE '%wikipedia.org%'
        AND url NOT LIKE '%wikimedia.org%'
        AND url NOT LIKE '%wikidata.org%'""").fetchone()[0]
    depth0 = c.execute("SELECT COUNT(*) FROM crawl_queue WHERE depth = 0").fetchone()[0]
    print(f"Queue total: {total_q}, non-wiki: {nonwiki}, depth-0 (high priority): {depth0}")
    c.close()

if __name__ == "__main__":
    main()

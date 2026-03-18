"""Re-populate the crawl queue from all links in the links table."""
from __future__ import annotations

import sqlite3
from urllib.parse import urlparse

DB = "data/platysearch.db"

def main() -> None:
    c = sqlite3.connect(DB)

    # Stats
    wiki = c.execute("SELECT COUNT(*) FROM pages WHERE url LIKE '%wikipedia.org%'").fetchone()[0]
    nonwiki = c.execute("SELECT COUNT(*) FROM pages WHERE url NOT LIKE '%wikipedia.org%'").fetchone()[0]
    total_links = c.execute("SELECT COUNT(*) FROM links").fetchone()[0]
    print(f"Wiki pages: {wiki}, Non-wiki pages: {nonwiki}")
    print(f"Total link rows: {total_links}")

    # Get all distinct target URLs not already crawled
    print("Finding uncrawled targets...")
    rows = c.execute(
        "SELECT DISTINCT l.target_url FROM links l "
        "LEFT JOIN pages p ON l.target_url = p.url "
        "WHERE p.id IS NULL"
    ).fetchall()
    print(f"Uncrawled distinct link targets: {len(rows)}")

    # Insert into crawl_queue
    inserted = 0
    for (url,) in rows:
        domain = urlparse(url).netloc
        if not domain:
            continue
        c.execute(
            "INSERT OR IGNORE INTO crawl_queue (url, domain, depth) VALUES (?, ?, ?)",
            (url, domain, 1),
        )
        inserted += 1
        if inserted % 50000 == 0:
            print(f"  Queued {inserted}...")
            c.commit()

    c.commit()
    queued = c.execute("SELECT COUNT(*) FROM crawl_queue").fetchone()[0]
    print(f"Inserted {inserted} URLs. Queue now: {queued}")

    # Show top queued domains
    rows = c.execute(
        "SELECT domain, COUNT(*) as cnt FROM crawl_queue GROUP BY domain ORDER BY cnt DESC LIMIT 25"
    ).fetchall()
    print("\nTop queued domains:")
    for domain, cnt in rows:
        print(f"  {domain}: {cnt}")

    c.close()

if __name__ == "__main__":
    main()

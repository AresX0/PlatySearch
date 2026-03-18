"""Fix crawl queue: prioritize known-live domains, deprioritize dead links."""
from __future__ import annotations

import sqlite3

DB = "data/platysearch.db"

# Known-live, high-quality domains we want at the front of the queue
PRIORITY_DOMAINS = {
    # Entertainment / Pop Culture
    "www.startrek.com", "memory-alpha.fandom.com", "www.starwars.com",
    "starwars.fandom.com", "www.disney.com", "disney.fandom.com",
    "www.marvel.com", "www.dc.com", "www.dndbeyond.com",
    "www.imdb.com", "www.rottentomatoes.com", "www.metacritic.com",
    "tvtropes.org", "www.tvguide.com",
    # News
    "www.reuters.com", "apnews.com", "www.bbc.com", "www.bbc.co.uk",
    "www.npr.org", "www.nytimes.com", "www.theguardian.com",
    "www.washingtonpost.com", "www.forbes.com", "variety.com",
    "www.hollywoodreporter.com", "deadline.com", "www.cnet.com",
    "www.theverge.com", "www.engadget.com", "techcrunch.com",
    # Science
    "www.nasa.gov", "science.nasa.gov", "www.jpl.nasa.gov",
    "hubblesite.org", "webb.nasa.gov", "www.spacex.com",
    "www.nature.com", "www.sciencedaily.com", "www.sciencenews.org",
    "www.scientificamerican.com", "www.newscientist.com",
    "phys.org", "www.popsci.com", "www.the-scientist.com",
    "www.science.org", "www.advancedsciencenews.com",
    "pubmed.ncbi.nlm.nih.gov", "www.ncbi.nlm.nih.gov",
    # Technology / Security
    "arstechnica.com", "www.wired.com", "krebsonsecurity.com",
    "thehackernews.com", "www.darkreading.com", "www.sans.org",
    "www.zdnet.com", "www.techrepublic.com",
    # Government
    "www.fbi.gov", "www.uscourts.gov", "www.dps.texas.gov",
    "www.loc.gov", "www.congress.gov",
    # Education
    "www.mit.edu", "news.mit.edu", "www.rice.edu", "news.rice.edu",
    "www.bcm.edu", "www.tcd.ie", "www.stanford.edu",
    "www.harvard.edu", "plato.stanford.edu", "www.britannica.com",
    # Reference / Archives
    "www.jstor.org", "www.oxforddnb.com",
    # Other quality domains
    "www.nationalgeographic.com", "www.smithsonianmag.com",
    "www.history.com", "www.livescience.com",
    "www.space.com", "www.astronomy.com",
}


def main() -> None:
    c = sqlite3.connect(DB, timeout=30)

    total = c.execute("SELECT COUNT(*) FROM crawl_queue").fetchone()[0]
    print(f"Queue size before cleanup: {total}")

    # 1) Set priority domains to depth 0
    updated = 0
    for domain in PRIORITY_DOMAINS:
        cur = c.execute(
            "UPDATE crawl_queue SET depth = 0 WHERE domain = ? AND depth > 0",
            (domain,),
        )
        updated += cur.rowcount
    c.commit()
    print(f"Prioritized {updated} URLs from {len(PRIORITY_DOMAINS)} live domains to depth 0")

    # 2) Remove obviously dead/low-value URLs (http:// old links, dead TLDs)
    dead_patterns = [
        "%.demon.co.uk%",  # Dead ISP
        "%.geocities.com%",  # Dead
        "%.angelfire.com%",  # Dead
        "%.tripod.com%",  # Dead
        "%.compuserve.com%",  # Dead
        "%granmai.co.cu%",
        "%www.compuart.ru%",
    ]
    removed = 0
    for pat in dead_patterns:
        cur = c.execute("DELETE FROM crawl_queue WHERE url LIKE ?", (pat,))
        removed += cur.rowcount
    c.commit()
    print(f"Removed {removed} URLs from known dead domains")

    # 3) Push all http:// (non-https) URLs to higher depth (less priority)
    cur = c.execute(
        "UPDATE crawl_queue SET depth = depth + 500 WHERE url LIKE 'http://%' AND depth < 500"
    )
    print(f"Deprioritized {cur.rowcount} insecure (http://) URLs")
    c.commit()

    # Stats after cleanup
    total = c.execute("SELECT COUNT(*) FROM crawl_queue").fetchone()[0]
    d0 = c.execute("SELECT COUNT(*) FROM crawl_queue WHERE depth = 0").fetchone()[0]
    d1 = c.execute("SELECT COUNT(*) FROM crawl_queue WHERE depth = 1").fetchone()[0]
    print(f"\nQueue after cleanup: {total} total, {d0} at depth 0, {d1} at depth 1")

    # Show what's at the front of the queue now
    rows = c.execute("""
        SELECT domain, COUNT(*) as cnt FROM crawl_queue 
        WHERE depth <= 1
        GROUP BY domain ORDER BY cnt DESC LIMIT 30
    """).fetchall()
    print("\nTop domains at depth 0-1:")
    for r in rows:
        print(f"  {r[0]}: {r[1]}")

    c.close()


if __name__ == "__main__":
    main()

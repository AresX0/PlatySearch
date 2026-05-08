"""Nightly scheduler — crawl, index, and score on a cron schedule."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from platysearch.config import get_settings

log = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None


# ── Job history tracking (DB-backed) ─────────────────────────────────────────


@dataclass
class JobRun:
    job_type: str  # "crawl", "index", "score", "full", "hourly_refresh"
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None
    status: str = "running"  # "running", "completed", "failed"
    pages_crawled: int = 0
    pages_indexed: int = 0
    pages_scored: int = 0
    error: str = ""
    db_id: int | None = None  # row id in job_history table


_current_job: JobRun | None = None


def get_current_job() -> JobRun | None:
    return _current_job


async def get_job_history() -> list[dict]:
    """Load job history from the database — persists across restarts."""
    from platysearch.database import load_job_history
    return await load_job_history(limit=20)


async def _start_job(job_type: str) -> JobRun:
    global _current_job
    job = JobRun(job_type=job_type)
    # Persist to DB immediately so it shows as "running".
    from platysearch.database import save_job
    job.db_id = await save_job(
        job_type=job.job_type,
        started_at=job.started_at.isoformat(),
        finished_at=None,
        status="running",
        pages_crawled=0,
        pages_indexed=0,
        pages_scored=0,
        error="",
    )
    _current_job = job
    return job


async def _update_job_progress(job: JobRun) -> None:
    """Persist current counters to DB (called periodically during long jobs)."""
    if job.db_id is None:
        return
    from platysearch.database import update_job
    await update_job(
        job.db_id,
        pages_crawled=job.pages_crawled,
        pages_indexed=job.pages_indexed,
        pages_scored=job.pages_scored,
    )


async def _finish_job(job: JobRun, status: str = "completed", error: str = "") -> None:
    global _current_job
    job.finished_at = datetime.now(timezone.utc)
    job.status = status
    job.error = error
    # Persist final state to DB.
    if job.db_id is not None:
        from platysearch.database import update_job
        await update_job(
            job.db_id,
            finished_at=job.finished_at.isoformat(),
            status=status,
            pages_crawled=job.pages_crawled,
            pages_indexed=job.pages_indexed,
            pages_scored=job.pages_scored,
            error=error,
        )
    if _current_job is job:
        _current_job = None


# ── Cancellation support ─────────────────────────────────────────────────────

_cancel_requested = False


def is_cancel_requested() -> bool:
    return _cancel_requested


async def request_stop() -> bool:
    """Request the current job to stop. Returns True if a job was running."""
    global _cancel_requested, _current_job
    if _current_job is None:
        return False
    _cancel_requested = True
    log.info("Stop requested for job: %s", _current_job.job_type)
    return True


# Seeds injected every nightly run so these sites are always re-crawled.
_NIGHTLY_SEEDS: list[str] = [
    # ── Platypus / Animals ──
    "https://en.wikipedia.org/wiki/Platypus",
    "https://www.worldwildlife.org/",
    "https://wwf.org.au/blogs/9-interesting-platypus-facts",
    "https://animals.sandiegozoo.org/animals/platypus",
    "https://animalia.bio/lists/platypus",
    "https://sandiegozoowildlifealliance.org/species/platypus",
    "https://bie.ala.org.au/species/Platypus",
    "https://www.pbs.org/wnet/nature/blog/platypus-fact-sheet",
    "https://wwf.org.au/",
    "https://animals.sandiegozoo.org/",
    "https://platypusspot.org/",
    # ── Dinosaurs / Paleontology ──
    "https://dinoanimals.com/dinosaurs/complete-dinosaurs-database/",
    "https://thedinosaurs.org/",
    "https://dinosaurencyclopedia.org/dinosaurs/",
    "https://animalia.bio/dinosauropedia",
    "https://www.dinosaurdendb.com/",
    "https://paleobiodb.org/#/",
    "https://www.uky.edu/KGS/education/education-links-dinosaure.php",
    "https://www.nhm.ac.uk/discover/dino-directory.html?site-version=desktop",
    # ── Sci-Fi / Star Trek / Firefly / Star Wars / Marvel / DC / Disney ──
    "https://www.startrek.com/",
    "https://www.startrek.com/news",
    "https://memory-alpha.fandom.com/wiki/Portal:Main",
    "https://memory-alpha.fandom.com/wiki/Star_Trek",
    "https://www.ex-astris-scientia.org/",
    "https://www.ex-astris-scientia.org/links.htm",
    "https://www.stargatearchive.com/",
    "https://www.thecompanion.app/",
    "https://www.starwars.com/",
    "https://www.starwars.com/news",
    "https://www.starwars.com/databank",
    "https://starwars.fandom.com/wiki/Main_Page",
    "https://starwars.fandom.com/wiki/Star_Wars",
    "https://theforce.net/",
    "https://www.starwarsnewsnet.com/",
    "https://www.marvel.com/",
    "https://www.marvel.com/characters",
    "https://www.marvel.com/comics",
    "https://www.dc.com/",
    "https://www.dc.com/characters",
    "https://www.dc.com/comics",
    "https://www.disney.com/",
    "https://www.disney.com/movies",
    "https://www.disney.com/shows",
    "https://disney.fandom.com/wiki/The_Disney_Wiki",
    "https://www.dndbeyond.com/",
    "https://www.imdb.com/",
    "https://www.rottentomatoes.com/",
    # ── Wikipedia — key topics ──
    "https://en.wikipedia.org/wiki/Main_Page",
    "https://en.wikipedia.org/wiki/Science_fiction",
    "https://en.wikipedia.org/wiki/Star_Trek",
    "https://en.wikipedia.org/wiki/History",
    "https://en.wikipedia.org/wiki/Hollywood",
    "https://en.wikipedia.org/wiki/Firefly_(TV_series)",
    "https://en.wikipedia.org/wiki/Marvel_Cinematic_Universe",
    "https://en.wikipedia.org/wiki/Animal",
    "https://en.wikipedia.org/wiki/Current_events",
    "https://en.wikipedia.org/wiki/Cybersecurity",
    "https://en.wikipedia.org/wiki/List_of_best-selling_books",
    "https://en.wikipedia.org/wiki/Isaac_Asimov",
    "https://en.wikipedia.org/wiki/Arthur_C._Clarke",
    "https://en.wikipedia.org/wiki/Ursula_K._Le_Guin",
    # ── US National / Local News ──
    "https://www.nbcnews.com/",
    "https://abcnews.go.com/",
    "https://www.usatoday.com/",
    "https://www.huffpost.com/",
    "https://www.usnews.com/",
    "https://www.usnews.com/news",
    "https://www.washingtonpost.com/",
    "https://www.nytimes.com/",
    "https://www.chicagotribune.com/",
    "https://www.chron.com/",
    "https://abc13.com/",
    "https://www.vox.com/",
    "https://www.politico.com/",
    "https://www.latimes.com/",
    "https://www.texastribune.org/",
    "https://www.miamiherald.com/",
    "https://www.nbclosangeles.com/",
    # ── International News / Global Wires ──
    "https://www.reuters.com/",
    "https://www.reuters.com/technology/",
    "https://www.reuters.com/world/",
    "https://apnews.com/",
    "https://apnews.com/politics",
    "https://apnews.com/science",
    "https://apnews.com/technology",
    "https://www.afp.com/",
    "http://www.xinhuanet.com/english/",
    "https://www.bbc.com/news",
    "https://www.bbc.com/news/science_and_environment",
    "https://www.bbc.com/news/technology",
    "https://www.theguardian.com/",
    "https://www.chinadaily.com.cn/",
    "https://www.scmp.com/",
    "https://www.aljazeera.com/",
    "https://www.euronews.com/",
    "https://www.dw.com/en/top-stories/s-9097",
    "https://www.npr.org/",
    "https://www.npr.org/sections/science/",
    "https://www.npr.org/sections/technology/",
    "https://www.npr.org/sections/news/",
    "https://www.npr.org/sections/world/",
    "https://www.pbs.org/",
    "https://www.pbs.org/newshour/",
    "https://www.pbs.org/newshour/science",
    "https://www.pbs.org/newshour/world",
    # ── News Aggregators / Financial News ──
    "https://www.bloomberg.com/",
    "https://www.cnbc.com/",
    "https://www.forbes.com/",
    # ── Science / Tech News ──
    "https://www.sciencedaily.com/",
    "https://www.newscientist.com/",
    "https://www.scientificamerican.com/",
    "https://www.the-scientist.com/",
    "https://www.sciencenews.org/",
    "https://www.science.org/news/latest-news",
    "https://phys.org/",
    "https://www.advancedsciencenews.com/",
    "https://www.popsci.com/",
    "https://www.nature.com/",
    "https://www.nature.com/news",
    "https://arstechnica.com/",
    "https://arstechnica.com/science/",
    "https://arstechnica.com/security/",
    "https://www.wired.com/",
    "https://www.wired.com/category/science/",
    "https://www.wired.com/category/security/",
    # ── Cybersecurity ──
    "https://www.darkreading.com/",
    "https://thehackernews.com/",
    "https://isc.sans.edu/",
    "https://www.sans.org/newsletters",
    "https://www.sans.org/blog/",
    "https://krebsonsecurity.com/",
    "https://onlinedegrees.sandiego.edu/top-cyber-security-blogs-websites/",
    "https://www.microsoft.com/en-us/security/blog/",
    "https://www.cve.org/",
    # ── Tech / Vendors ──
    "https://www.apple.com/",
    # ── Universities / Research ──
    "https://www.mit.edu/",
    "https://news.mit.edu/",
    "https://www.tcd.ie/",
    "https://www.tcd.ie/research/",
    "https://www.bcm.edu/",
    "https://www.bcm.edu/research",
    "https://www.rice.edu/",
    "https://news.rice.edu/",
    # ── Libraries ──
    "https://libraries.mit.edu/",
    "https://library.harvard.edu/",
    "https://www.library.rice.edu/",
    "https://library.tmc.edu/",
    "https://www.bodleian.ox.ac.uk/",
    # ── Academic Search / Scholarly ──
    "https://pubmed.ncbi.nlm.nih.gov/",
    "https://www.ncbi.nlm.nih.gov/pmc/",
    "https://scholar.google.com/",
    "https://www.microsoft.com/en-us/research/",
    "https://www.microsoft.com/en-us/research/research-area/",
    "https://www.microsoft.com/en-us/research/publications/",
    "https://www.crossref.org/",
    "https://www.crossref.org/blog/",
    "https://search.crossref.org/",
    "https://www.lens.org/",
    "https://www.lens.org/lens/search/scholar/list",
    "https://openalex.org/",
    "https://docs.openalex.org/",
    "https://www.scopus.com/",
    "https://www.elsevier.com/products/scopus",
    "https://clarivate.com/products/scientific-and-academic-research/research-discovery-and-workflow-solutions/webofscience-platform/",
    "https://mjl.clarivate.com/home",
    "https://www.embase.com/",
    "https://www.elsevier.com/products/embase",
    "https://doaj.org/",
    "https://doaj.org/search/journals",
    "https://doaj.org/search/articles",
    "https://mjl.clarivate.com/search-results?issn=&hide_exact_match_fl=true&utm_source=mjl&utm_medium=share-by-link&utm_campaign=search-results-share-this-journal&utm_content=sciedition",
    "https://abdc.edu.au/abdc-journal-quality-list/",
    "https://www.ugc.gov.in/journallist/",
    "https://ugccare.unipune.ac.in/Apps1/User/WebA/SearchList",
    "https://listofjournals.com/sci.php",
    "https://listofjournals.com/",
    # ── NASA & Space ──
    "https://www.nasa.gov/",
    "https://www.nasa.gov/news/",
    "https://www.nasa.gov/missions/",
    "https://www.nasa.gov/humans-in-space/",
    "https://www.nasa.gov/solar-system/",
    "https://www.nasa.gov/universe/",
    "https://www.nasa.gov/earth/",
    "https://www.nasa.gov/technology/",
    "https://science.nasa.gov/",
    "https://science.nasa.gov/mars/",
    "https://science.nasa.gov/exoplanets/",
    "https://www.jpl.nasa.gov/",
    "https://www.jpl.nasa.gov/news",
    "https://hubblesite.org/",
    "https://webb.nasa.gov/",
    "https://www.spacex.com/",
    # ── Government / Legal ──
    "https://www.fbi.gov/",
    "https://www.fbi.gov/how-we-can-help-you/more-fbi-services-and-information/identity-history-summary-checks/state-maintained-records-listing",
    "https://www.uscourts.gov/court-records",
    "https://www.dps.texas.gov/section/crime-records",
    # ── Social (preferred) ──
    "https://www.youtube.com/",
    "https://bsky.app/profile/platypusmatch.bsky.social",
    "https://bsky.app/profile/anotherfrakkinpodcast.bsky.social",
    "https://www.threads.net/",
    # ── Reference ──
    "https://www.britannica.com/",
    "https://www.loc.gov/",
]

# Image-rich sites crawled nightly to populate the Images tab.
_IMAGE_SEEDS: list[str] = [
    "https://www.nationalgeographic.com/animals",
    "https://www.nationalgeographic.com/photography/",
    "https://www.nationalgeographic.com/science/",
    "https://www.nasa.gov/image-of-the-day/",
    "https://www.nasa.gov/images/",
    "https://hubblesite.org/images/gallery",
    "https://webb.nasa.gov/content/multimedia/images.html",
    "https://www.nature.com/nature/articles?type=news",
    "https://www.sciencedaily.com/news/",
    "https://www.nhm.ac.uk/discover/dino-directory.html",
    "https://dinoanimals.com/dinosaurs/complete-dinosaurs-database/",
    "https://animalia.bio/dinosauropedia",
    "https://www.worldwildlife.org/species",
    "https://animals.sandiegozoo.org/animals",
    "https://www.pbs.org/wnet/nature/",
    "https://www.flickr.com/commons",
    "https://commons.wikimedia.org/wiki/Main_Page",
    "https://unsplash.com/",
    "https://www.smithsonianmag.com/photocontest/",
    "https://www.bbc.com/news/in_pictures",
]


# ── Daily news / cybersecurity RSS feeds ─────────────────────────────────────
#
# Pulled fresh every day at 10:00 AM America/Chicago.  These are the official
# feeds for the news, science, and cybersecurity sites we track.  Article URLs
# extracted from each feed are enqueued for crawling and then indexed/scored.

_NEWS_FEEDS: list[str] = [
    # ── General / national news ──
    "https://feeds.npr.org/1001/rss.xml",            # NPR top stories
    "https://feeds.npr.org/1004/rss.xml",            # NPR world
    "https://feeds.npr.org/1019/rss.xml",            # NPR technology
    "https://feeds.npr.org/1007/rss.xml",            # NPR science
    "https://www.pbs.org/newshour/feeds/rss/headlines",
    "https://www.pbs.org/newshour/feeds/rss/world",
    "https://www.pbs.org/newshour/feeds/rss/science",
    "https://feeds.bbci.co.uk/news/rss.xml",
    "https://feeds.bbci.co.uk/news/world/rss.xml",
    "https://feeds.bbci.co.uk/news/technology/rss.xml",
    "https://feeds.bbci.co.uk/news/science_and_environment/rss.xml",
    "https://www.theguardian.com/world/rss",
    "https://www.theguardian.com/us-news/rss",
    "https://www.theguardian.com/technology/rss",
    "https://www.theguardian.com/science/rss",
    "https://www.aljazeera.com/xml/rss/all.xml",
    "https://feeds.washingtonpost.com/rss/world",
    "https://feeds.washingtonpost.com/rss/national",
    "https://feeds.washingtonpost.com/rss/business/technology",
    "https://rss.nytimes.com/services/xml/rss/nyt/HomePage.xml",
    "https://rss.nytimes.com/services/xml/rss/nyt/World.xml",
    "https://rss.nytimes.com/services/xml/rss/nyt/Technology.xml",
    "https://rss.nytimes.com/services/xml/rss/nyt/Science.xml",
    "https://feeds.reuters.com/reuters/topNews",
    "https://feeds.reuters.com/reuters/worldNews",
    "https://feeds.reuters.com/reuters/technologyNews",
    "https://www.huffpost.com/section/front-page/feed",
    "https://www.politico.com/rss/politicopicks.xml",
    "https://www.vox.com/rss/index.xml",
    "https://www.texastribune.org/feeds/all/",
    # ── Wires / international ──
    "https://apnews.com/index.rss",
    "https://www.dw.com/atom/rss-en-top",
    "https://www.euronews.com/rss",
    # ── Science / tech news ──
    "https://www.sciencedaily.com/rss/all.xml",
    "https://www.newscientist.com/feed/home/",
    "https://rss.sciam.com/ScientificAmerican-Global",
    "https://www.sciencenews.org/feed",
    "https://phys.org/rss-feed/",
    "https://www.nature.com/nature.rss",
    "https://feeds.arstechnica.com/arstechnica/index",
    "https://feeds.arstechnica.com/arstechnica/science",
    "https://feeds.arstechnica.com/arstechnica/security",
    "https://www.wired.com/feed/rss",
    "https://www.wired.com/feed/category/science/latest/rss",
    "https://www.wired.com/feed/category/security/latest/rss",
    "https://www.popsci.com/feed/",
    # ── Cybersecurity ──
    "https://www.darkreading.com/rss.xml",
    "https://feeds.feedburner.com/TheHackersNews",
    "https://krebsonsecurity.com/feed/",
    "https://isc.sans.edu/rssfeed.xml",
    "https://www.sans.org/blog/feed.xml",
    "https://www.microsoft.com/en-us/security/blog/feed/",
    "https://www.cisa.gov/news.xml",
    "https://www.cisa.gov/cybersecurity-advisories/all.xml",
    "https://www.bleepingcomputer.com/feed/",
    "https://www.schneier.com/feed/atom/",
    "https://www.csoonline.com/feed/",
    "https://www.securityweek.com/feed/",
    # ── Aggregators (HN front page) ──
    "https://hnrss.org/frontpage",
    "https://hnrss.org/newest?points=100",
]


async def _fetch_news_urls_from_feeds(
    feeds: list[str],
    per_feed_limit: int = 60,
    timeout_secs: int = 15,
) -> list[str]:
    """Fetch each RSS/Atom feed and return a deduplicated list of article URLs."""
    import aiohttp
    import feedparser

    settings = get_settings()
    headers = {"User-Agent": settings.user_agent}
    timeout = aiohttp.ClientTimeout(total=timeout_secs)

    seen: set[str] = set()
    urls: list[str] = []

    sem = asyncio.Semaphore(8)

    async def _one(session: aiohttp.ClientSession, feed_url: str) -> list[str]:
        async with sem:
            try:
                async with session.get(feed_url, allow_redirects=True) as resp:
                    if resp.status != 200:
                        log.info("Feed %s returned HTTP %d", feed_url, resp.status)
                        return []
                    body = await resp.read()
            except Exception as exc:
                log.info("Feed %s failed: %s", feed_url, exc)
                return []
        # feedparser is sync — run in thread to avoid blocking the loop.
        parsed = await asyncio.to_thread(feedparser.parse, body)
        out: list[str] = []
        for entry in parsed.entries[:per_feed_limit]:
            link = getattr(entry, "link", None)
            if link and isinstance(link, str) and link.startswith(("http://", "https://")):
                out.append(link)
        return out

    import ssl as _ssl
    ssl_ctx = _ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = _ssl.CERT_NONE
    connector = aiohttp.TCPConnector(limit=16, ssl=ssl_ctx)

    async with aiohttp.ClientSession(
        connector=connector, timeout=timeout, headers=headers,
    ) as session:
        results = await asyncio.gather(
            *[_one(session, f) for f in feeds], return_exceptions=False,
        )

    for batch in results:
        for u in batch:
            if u not in seen:
                seen.add(u)
                urls.append(u)
    return urls


async def _daily_news_refresh() -> None:
    """Pull fresh article URLs from RSS feeds and crawl/index them.

    Scheduled daily at 10:00 AM America/Chicago.  Skipped if another job
    is already running (will run on the next cron tick or be picked up
    by the next nightly crawl).
    """
    global _nightly_running, _cancel_requested
    if _current_job is not None:
        log.warning(
            "Daily news refresh skipped — a job is already running: %s",
            _current_job.job_type,
        )
        return

    import asyncio as _asyncio

    from platysearch.ai_detector import score_all_pages
    from platysearch.crawler import crawl
    from platysearch.indexer import compute_link_scores, index_all_pages

    _nightly_running = True
    _cancel_requested = False
    job = await _start_job("daily_news")
    t0 = time.monotonic()
    try:
        log.info("Daily news refresh: fetching %d RSS feeds…", len(_NEWS_FEEDS))
        try:
            article_urls = await _asyncio.wait_for(
                _fetch_news_urls_from_feeds(_NEWS_FEEDS),
                timeout=300,  # 5 min hard cap on the whole feed-pull phase
            )
        except _asyncio.TimeoutError:
            article_urls = []
            log.warning("Feed-pull phase timed out after 5 min.")

        log.info(
            "Daily news refresh: discovered %d article URLs in %.0fs.",
            len(article_urls), time.monotonic() - t0,
        )

        if article_urls:
            # Cap so a runaway feed can't blow the budget.
            max_articles = min(len(article_urls), 2000)
            crawl_t0 = time.monotonic()
            try:
                count = await crawl(
                    seeds=article_urls[:max_articles],
                    max_pages=max_articles,
                    max_seconds=30 * 60,  # 30 min crawl cap
                    cancel_check=is_cancel_requested,
                )
                job.pages_crawled = count
                await _update_job_progress(job)
                log.info(
                    "Daily news crawl finished: %d pages fetched in %.0fs.",
                    count, time.monotonic() - crawl_t0,
                )
            except Exception as exc:
                log.exception("Daily news crawl failed: %s", exc)
                job.error = f"Crawl error: {exc}"
                await _update_job_progress(job)

        # Re-index + re-score so new articles surface immediately.
        try:
            job.pages_indexed = await index_all_pages()
            await compute_link_scores()
            await _update_job_progress(job)
        except Exception:
            log.exception("Daily news indexing failed.")

        try:
            job.pages_scored = await score_all_pages()
            await _update_job_progress(job)
        except Exception:
            log.exception("Daily news scoring failed.")

        _persist_db(get_settings().db_path)

        if _cancel_requested:
            await _finish_job(job, status="cancelled", error="Stopped by admin")
        else:
            await _finish_job(job)
    except Exception as exc:
        await _finish_job(job, status="failed", error=str(exc))
        raise
    finally:
        _nightly_running = False
        _cancel_requested = False


async def _nightly_update() -> None:
    """Run a crawl cycle followed by re-indexing and scoring."""
    global _nightly_running, _cancel_requested
    if _current_job is not None:
        log.warning("Skipping scheduled crawl — a job is already running: %s", _current_job.job_type)
        return
    _nightly_running = True
    _cancel_requested = False
    job = await _start_job("full")
    try:
        await _nightly_update_inner(job)
        if _cancel_requested:
            await _finish_job(job, status="cancelled", error="Stopped by admin")
        else:
            await _finish_job(job)
    except Exception as exc:
        await _finish_job(job, status="failed", error=str(exc))
        raise
    finally:
        _nightly_running = False
        _cancel_requested = False


async def _nightly_update_inner(job: JobRun) -> None:
    """Core nightly logic — crawl (1 h hard cap), then index + score."""
    global _cancel_requested
    from platysearch.ai_detector import score_all_pages
    from platysearch.crawler import crawl
    from platysearch.indexer import compute_link_scores, index_all_pages

    settings = get_settings()
    max_db_mb = settings.max_db_size_mb

    # ── Check if crawl is disabled ───────────────────────────────────────
    if not settings.crawl_enabled:
        log.info("Crawl is disabled (PLATY_CRAWL_ENABLED=false). Skipping to index/score.")
        job.pages_indexed = await index_all_pages()
        await _update_job_progress(job)
        await compute_link_scores()
        job.pages_scored = await score_all_pages()
        await _update_job_progress(job)
        _persist_db(settings.db_path)
        return

    if _cancel_requested:
        log.info("Cancel requested before crawl started — skipping to index/score.")

    # ── Check DB size ────────────────────────────────────────────────────
    db_path = settings.db_path
    if not _cancel_requested and db_path.exists():
        size_mb = db_path.stat().st_size / (1024 * 1024)
        log.info("Current DB size: %.1f MB (limit: %d MB)", size_mb, max_db_mb)
        if size_mb >= max_db_mb:
            log.warning("DB size %.1f MB exceeds limit %d MB — skipping crawl.", size_mb, max_db_mb)

    skip_crawl = _cancel_requested or (
        db_path.exists() and db_path.stat().st_size / (1024 * 1024) >= max_db_mb
    )

    if not skip_crawl:
        nightly_pages = settings.nightly_crawl_pages

        # ── Merge all seeds (nightly + image + custom) into one crawl ────
        from platysearch.database import get_custom_seed_urls
        custom = await get_custom_seed_urls()
        nightly_set = set(_NIGHTLY_SEEDS)
        all_seeds = (
            list(_NIGHTLY_SEEDS)
            + list(_IMAGE_SEEDS)
            + [u for u in custom if u not in nightly_set]
        )

        # Hard cap: 1 hour for the entire crawl phase.
        crawl_budget_secs = 60 * 60  # 1 h
        crawl_t0 = time.monotonic()

        log.info(
            "Crawl: fetching up to %d pages with %d seeds (max %ds)…",
            nightly_pages, len(all_seeds), crawl_budget_secs,
        )
        try:
            count = await crawl(
                seeds=all_seeds,
                max_pages=nightly_pages,
                max_seconds=crawl_budget_secs,
                cancel_check=is_cancel_requested,
            )
            job.pages_crawled = count
            await _update_job_progress(job)
            log.info(
                "Crawl finished: %d pages fetched in %.0fs.",
                count, time.monotonic() - crawl_t0,
            )
        except Exception as exc:
            log.exception("Crawl failed: %s", exc)
            job.error = f"Crawl error: {exc}"
            await _update_job_progress(job)

        # Persist after crawl so progress survives a crash during index/score.
        _persist_db(settings.db_path)

        if _cancel_requested:
            log.info("Crawl stopped by admin after %d pages — proceeding to index.", job.pages_crawled)

    # ── Always index & score (even after cancel/stop) ────────────────────
    _cancel_requested = False  # Reset so index/score runs to completion
    log.info("Starting index + score phase…")
    try:
        job.pages_indexed = await index_all_pages()
        await compute_link_scores()
        await _update_job_progress(job)
        log.info("Indexing complete: %d pages indexed.", job.pages_indexed)
    except Exception:
        log.exception("Indexing failed.")

    try:
        job.pages_scored = await score_all_pages()
        await _update_job_progress(job)
        log.info("Scoring complete: %d pages scored.", job.pages_scored)
    except Exception:
        log.exception("Scoring failed.")

    # ── Persist DB to Azure /home/data so it survives container restarts ─
    _persist_db(settings.db_path)


def _persist_db(db_path: os.PathLike[str] | str) -> None:
    """Copy the live DB to Azure persistent storage (/home/data/)."""
    import pathlib
    import shutil

    persist_dir = pathlib.Path("/home/data")
    if not persist_dir.exists():
        return  # not running on Azure App Service
    try:
        src = pathlib.Path(db_path)
        if src.exists():
            shutil.copy2(src, persist_dir / "platysearch.db")
            log.info("DB persisted to %s (%.1f MB).", persist_dir, src.stat().st_size / (1024 * 1024))
    except Exception:
        log.exception("Failed to persist DB to %s.", persist_dir)


# Track whether the nightly crawl is running so hourly refresh skips overlap.
_nightly_running = False


async def _hourly_refresh() -> None:
    """Re-index and re-score so new pages appear in results quickly."""
    if _nightly_running:
        log.info("Hourly refresh skipped \u2014 nightly update is running.")
        return

    from platysearch.ai_detector import score_all_pages
    from platysearch.database import get_db, get_meta
    from platysearch.indexer import compute_link_scores, index_all_pages

    # ── Fast skip when no new pages have arrived since last index ──
    db = await get_db()
    try:
        cur = await db.execute("SELECT COALESCE(MAX(id), 0) FROM pages")
        row = await cur.fetchone()
        current_max = row[0] if row else 0
    finally:
        await db.close()
    last_max_str = await get_meta("last_indexed_max_page_id")
    last_max = int(last_max_str) if last_max_str and last_max_str.isdigit() else 0
    if current_max > 0 and current_max == last_max:
        log.info(
            "Hourly refresh: no new pages since last index (max id %d) \u2014 skipping.",
            current_max,
        )
        return

    job = await _start_job("hourly_refresh")
    log.info(
        "Hourly refresh: re-indexing and scoring (last=%d, current=%d)\u2026",
        last_max, current_max,
    )
    t0 = time.monotonic()
    try:
        job.pages_indexed = await index_all_pages()
        await compute_link_scores()
        job.pages_scored = await score_all_pages()
        elapsed = time.monotonic() - t0
        log.info("Hourly refresh complete: %d pages scored in %.0fs.", job.pages_scored, elapsed)
        await _update_job_progress(job)
        _persist_db(get_settings().db_path)
        await _finish_job(job)
    except Exception as exc:
        log.exception("Hourly refresh failed.")
        await _finish_job(job, status="failed", error=str(exc))


def start_scheduler() -> AsyncIOScheduler:
    """Create and start the nightly update scheduler. Returns the scheduler."""
    global _scheduler
    if _scheduler is not None:
        return _scheduler

    settings = get_settings()
    _scheduler = AsyncIOScheduler()

    import datetime

    _scheduler.add_job(
        _nightly_update,
        trigger=IntervalTrigger(hours=4),
        id="nightly_update",
        name="Crawl + index + score (every 4h)",
        replace_existing=True,
        next_run_time=datetime.datetime.now(datetime.timezone.utc),
    )

    _scheduler.add_job(
        _hourly_refresh,
        trigger=IntervalTrigger(minutes=30),
        id="half_hourly_refresh",
        name="Re-index + score (every 30 min)",
        replace_existing=True,
    )

    # Daily news / cybersecurity refresh from RSS feeds — 10:00 AM America/Chicago.
    try:
        from zoneinfo import ZoneInfo
        chicago_tz = ZoneInfo("America/Chicago")
    except Exception:
        log.warning("Could not load America/Chicago timezone; falling back to UTC.")
        chicago_tz = datetime.timezone.utc

    _scheduler.add_job(
        _daily_news_refresh,
        trigger=CronTrigger(hour=10, minute=0, timezone=chicago_tz),
        id="daily_news_refresh",
        name="Daily news refresh from RSS feeds (10:00 AM Central)",
        replace_existing=True,
    )

    _scheduler.start()
    log.info(
        "Scheduler started - crawl every 4 hours (first run NOW), "
        "re-index every 30 min, news refresh daily at 10:00 AM Central",
    )
    return _scheduler


def stop_scheduler() -> None:
    """Shut down the scheduler gracefully."""
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        log.info("Scheduler stopped.")


# ── Manual triggers ──────────────────────────────────────────────────────────


async def trigger_crawl() -> JobRun:
    """Manually trigger a full crawl + index + score cycle."""
    if _current_job is not None:
        raise RuntimeError(f"A job is already running: {_current_job.job_type}")
    import asyncio
    job_ref: list[JobRun] = []

    async def _run() -> None:
        global _nightly_running, _cancel_requested
        _nightly_running = True
        _cancel_requested = False
        job = await _start_job("manual_crawl")
        job_ref.append(job)
        try:
            await _nightly_update_inner(job)
            if _cancel_requested:
                await _finish_job(job, status="cancelled", error="Stopped by admin")
            else:
                await _finish_job(job)
        except Exception as exc:
            await _finish_job(job, status="failed", error=str(exc))
        finally:
            _nightly_running = False
            _cancel_requested = False

    asyncio.create_task(_run())
    # Wait briefly for the job object to be created.
    await asyncio.sleep(0.1)
    return job_ref[0] if job_ref else _current_job  # type: ignore[return-value]


async def trigger_index() -> JobRun:
    """Manually trigger an index + score cycle (no crawl)."""
    if _current_job is not None:
        raise RuntimeError(f"A job is already running: {_current_job.job_type}")
    import asyncio

    from platysearch.ai_detector import score_all_pages
    from platysearch.indexer import compute_link_scores, index_all_pages

    job = await _start_job("manual_index")

    async def _run() -> None:
        global _cancel_requested
        _cancel_requested = False
        try:
            job.pages_indexed = await index_all_pages()
            await _update_job_progress(job)
            if _cancel_requested:
                await _finish_job(job, status="cancelled", error="Stopped by admin")
                return
            await compute_link_scores()
            job.pages_scored = await score_all_pages()
            await _update_job_progress(job)
            _persist_db(get_settings().db_path)
            if _cancel_requested:
                await _finish_job(job, status="cancelled", error="Stopped by admin")
            else:
                await _finish_job(job)
        except Exception as exc:
            await _finish_job(job, status="failed", error=str(exc))
        finally:
            _cancel_requested = False

    asyncio.create_task(_run())
    return job


def get_next_run_times() -> dict[str, datetime | None]:
    """Return the next scheduled run time for each job."""
    if _scheduler is None:
        return {}
    result = {}
    for j in _scheduler.get_jobs():
        result[j.id] = j.next_run_time
    return result

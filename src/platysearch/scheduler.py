"""Nightly scheduler — crawl, index, and score on a cron schedule."""

from __future__ import annotations

import logging
import os
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from platysearch.config import get_settings

log = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None


# ── Job history tracking ─────────────────────────────────────────────────────


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


_job_history: deque[JobRun] = deque(maxlen=20)
_current_job: JobRun | None = None


def get_current_job() -> JobRun | None:
    return _current_job


def get_job_history() -> list[JobRun]:
    return list(_job_history)


def _start_job(job_type: str) -> JobRun:
    global _current_job
    job = JobRun(job_type=job_type)
    _current_job = job
    return job


def _finish_job(job: JobRun, status: str = "completed", error: str = "") -> None:
    global _current_job
    job.finished_at = datetime.now(timezone.utc)
    job.status = status
    job.error = error
    _job_history.appendleft(job)
    if _current_job is job:
        _current_job = None

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
    # ── Universities / Research ──
    "https://www.mit.edu/",
    "https://news.mit.edu/",
    "https://www.tcd.ie/",
    "https://www.tcd.ie/research/",
    "https://www.bcm.edu/",
    "https://www.bcm.edu/research",
    "https://www.rice.edu/",
    "https://news.rice.edu/",
    "https://pubmed.ncbi.nlm.nih.gov/",
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


async def _nightly_update() -> None:
    """Run a crawl cycle followed by re-indexing and scoring."""
    global _nightly_running
    _nightly_running = True
    job = _start_job("full")
    try:
        await _nightly_update_inner(job)
        _finish_job(job)
    except Exception as exc:
        _finish_job(job, status="failed", error=str(exc))
        raise
    finally:
        _nightly_running = False


async def _nightly_update_inner(job: JobRun) -> None:
    """Core nightly logic — crawl, index, score."""
    from platysearch.ai_detector import score_all_pages
    from platysearch.crawler import crawl
    from platysearch.database import get_db
    from platysearch.indexer import compute_link_scores, index_all_pages

    settings = get_settings()
    max_db_mb = settings.max_db_size_mb

    # ── Check if crawl is disabled ───────────────────────────────────────
    if not settings.crawl_enabled:
        log.info("Crawl is disabled (PLATY_CRAWL_ENABLED=false). Skipping to index/score.")
        job.pages_indexed = await index_all_pages()
        await compute_link_scores()
        job.pages_scored = await score_all_pages()
        _persist_db(settings.db_path)
        return

    # ── Check DB size ────────────────────────────────────────────────────
    db_path = settings.db_path
    if db_path.exists():
        size_mb = db_path.stat().st_size / (1024 * 1024)
        log.info("Current DB size: %.1f MB (limit: %d MB)", size_mb, max_db_mb)
        if size_mb >= max_db_mb:
            log.warning("DB size %.1f MB exceeds limit %d MB — skipping crawl.", size_mb, max_db_mb)
            job.pages_indexed = await index_all_pages()
            await compute_link_scores()
            job.pages_scored = await score_all_pages()
            return

    nightly_pages = settings.nightly_crawl_pages

    # ── Crawl with seeds — ensures all key sites are re-visited ──────────
    # ── Merge custom seeds from DB ─────────────────────────────────────────
    from platysearch.database import get_custom_seed_urls
    custom = await get_custom_seed_urls()
    all_seeds = list(_NIGHTLY_SEEDS) + [u for u in custom if u not in set(_NIGHTLY_SEEDS)]

    log.info("Nightly crawl: fetching up to %d pages with %d seeds…", nightly_pages, len(all_seeds))
    try:
        count = await crawl(seeds=all_seeds, max_pages=nightly_pages)
        job.pages_crawled += count
        log.info("Nightly crawl finished: %d pages fetched.", count)
    except Exception:
        log.exception("Nightly crawl failed.")

    # ── Image crawl — small batch of image-rich sites (~1 hour) ──────────
    image_pages = max(500, nightly_pages // 4)
    log.info("Nightly image crawl: fetching up to %d pages from %d image seeds…", image_pages, len(_IMAGE_SEEDS))
    try:
        img_count = await crawl(seeds=list(_IMAGE_SEEDS), max_pages=image_pages)
        job.pages_crawled += img_count
        log.info("Nightly image crawl finished: %d pages fetched.", img_count)
    except Exception:
        log.exception("Nightly image crawl failed.")

    # ── Index & Score ────────────────────────────────────────────────────
    try:
        job.pages_indexed = await index_all_pages()
        await compute_link_scores()
        log.info("Nightly indexing complete.")
    except Exception:
        log.exception("Nightly indexing failed.")

    try:
        job.pages_scored = await score_all_pages()
        log.info("Nightly scoring complete: %d pages scored.", job.pages_scored)
    except Exception:
        log.exception("Nightly scoring failed.")

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
        log.info("Hourly refresh skipped — nightly update is running.")
        return

    from platysearch.ai_detector import score_all_pages
    from platysearch.indexer import compute_link_scores, index_all_pages

    job = _start_job("hourly_refresh")
    log.info("Hourly refresh: re-indexing and scoring…")
    t0 = time.monotonic()
    try:
        job.pages_indexed = await index_all_pages()
        await compute_link_scores()
        job.pages_scored = await score_all_pages()
        elapsed = time.monotonic() - t0
        log.info("Hourly refresh complete: %d pages scored in %.0fs.", job.pages_scored, elapsed)
        _persist_db(get_settings().db_path)
        _finish_job(job)
    except Exception as exc:
        log.exception("Hourly refresh failed.")
        _finish_job(job, status="failed", error=str(exc))


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
        trigger=IntervalTrigger(hours=1),
        id="hourly_refresh",
        name="Hourly re-index + score",
        replace_existing=True,
    )

    _scheduler.start()
    log.info(
        "Scheduler started - crawl every 4 hours (first run NOW), hourly refresh enabled",
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
        global _nightly_running
        _nightly_running = True
        job = _start_job("manual_crawl")
        job_ref.append(job)
        try:
            await _nightly_update_inner(job)
            _finish_job(job)
        except Exception as exc:
            _finish_job(job, status="failed", error=str(exc))
        finally:
            _nightly_running = False

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

    job = _start_job("manual_index")

    async def _run() -> None:
        try:
            job.pages_indexed = await index_all_pages()
            await compute_link_scores()
            job.pages_scored = await score_all_pages()
            _persist_db(get_settings().db_path)
            _finish_job(job)
        except Exception as exc:
            _finish_job(job, status="failed", error=str(exc))

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

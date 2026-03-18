"""Nightly scheduler — crawl, index, and score on a cron schedule."""

from __future__ import annotations

import logging
import os

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from platysearch.config import get_settings

log = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None


async def _nightly_update() -> None:
    """Run a crawl cycle followed by re-indexing and scoring."""
    from platysearch.ai_detector import score_all_pages
    from platysearch.crawler import crawl
    from platysearch.database import get_db
    from platysearch.indexer import compute_link_scores, index_all_pages

    settings = get_settings()
    max_db_mb = settings.max_db_size_mb

    # ── Check DB size ────────────────────────────────────────────────────
    db_path = settings.db_path
    if db_path.exists():
        size_mb = db_path.stat().st_size / (1024 * 1024)
        log.info("Current DB size: %.1f MB (limit: %d MB)", size_mb, max_db_mb)
        if size_mb >= max_db_mb:
            log.warning("DB size %.1f MB exceeds limit %d MB — skipping crawl.", size_mb, max_db_mb)
            # Still re-index and score existing content.
            await index_all_pages()
            await compute_link_scores()
            await score_all_pages()
            return

    # ── Check if queue has URLs ──────────────────────────────────────────
    db = await get_db()
    try:
        row = await db.execute_fetchall("SELECT COUNT(*) FROM crawl_queue")
        queue_count = row[0][0] if row else 0
    finally:
        await db.close()

    nightly_pages = settings.nightly_crawl_pages

    if queue_count == 0:
        log.info("Crawl queue empty — seeding from existing outgoing links.")
        db = await get_db()
        try:
            # Re-seed queue from discovered links not yet crawled.
            await db.execute(
                """INSERT OR IGNORE INTO crawl_queue (url, domain, depth)
                   SELECT DISTINCT l.target_url,
                          SUBSTR(l.target_url,
                                 INSTR(l.target_url, '://') + 3,
                                 INSTR(SUBSTR(l.target_url, INSTR(l.target_url, '://') + 3), '/') - 1),
                          1
                   FROM links l
                   LEFT JOIN pages p ON p.url = l.target_url
                   WHERE p.id IS NULL
                   LIMIT ?""",
                (nightly_pages * 3,),
            )
            await db.commit()
        finally:
            await db.close()

    # ── Crawl ────────────────────────────────────────────────────────────
    log.info("Nightly crawl: fetching up to %d pages…", nightly_pages)
    try:
        count = await crawl(seeds=[], max_pages=nightly_pages)
        log.info("Nightly crawl finished: %d pages fetched.", count)
    except Exception:
        log.exception("Nightly crawl failed.")

    # ── Index & Score ────────────────────────────────────────────────────
    try:
        await index_all_pages()
        await compute_link_scores()
        log.info("Nightly indexing complete.")
    except Exception:
        log.exception("Nightly indexing failed.")

    try:
        scored = await score_all_pages()
        log.info("Nightly scoring complete: %d pages scored.", scored)
    except Exception:
        log.exception("Nightly scoring failed.")


def start_scheduler() -> AsyncIOScheduler:
    """Create and start the nightly update scheduler. Returns the scheduler."""
    global _scheduler
    if _scheduler is not None:
        return _scheduler

    settings = get_settings()
    _scheduler = AsyncIOScheduler()

    _scheduler.add_job(
        _nightly_update,
        trigger=CronTrigger(
            hour=settings.scheduler_hour,
            minute=settings.scheduler_minute,
            timezone=settings.scheduler_timezone,
        ),
        id="nightly_update",
        name="Nightly crawl + index + score",
        replace_existing=True,
    )

    _scheduler.start()
    log.info(
        "Scheduler started — nightly update at %02d:%02d %s",
        settings.scheduler_hour,
        settings.scheduler_minute,
        settings.scheduler_timezone,
    )
    return _scheduler


def stop_scheduler() -> None:
    """Shut down the scheduler gracefully."""
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        log.info("Scheduler stopped.")

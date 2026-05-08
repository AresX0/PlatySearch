"""Live news feed — dynamically poll RSS feeds at search time.

When a user searches with the ``news`` tab, we want fresh results even for
articles the crawler hasn't visited yet.  This module maintains a small
in-memory, TTL-cached pool of recent RSS entries from the same feeds the
nightly news refresh uses, and lets the ranker filter that pool by query.

The actual indexing/crawling still happens in the background (the daily
news refresh enqueues these URLs for full crawl), but live search can
surface fresh headlines immediately.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from html import unescape
from urllib.parse import urlparse

import aiohttp

from platysearch.config import get_settings

log = logging.getLogger(__name__)


# How long (seconds) to keep cached feed entries in memory before refetching.
_CACHE_TTL_SECS = 10 * 60  # 10 min
# Cap how many entries we hold in memory total.
_MAX_ENTRIES = 5000
# Max time we'll spend fetching feeds during a single refresh.
_FETCH_TIMEOUT_SECS = 12
# Per-feed entry cap.
_PER_FEED_LIMIT = 40

_TAG_RE = re.compile(r"<[^>]+>")


@dataclass(frozen=True)
class LiveNewsEntry:
    url: str
    title: str
    summary: str
    domain: str
    published_ts: float  # unix epoch seconds; 0 if unknown


# ── Cache state ──────────────────────────────────────────────────────────────

_cache: list[LiveNewsEntry] = []
_cache_loaded_at: float = 0.0
_refresh_lock = asyncio.Lock()
_refresh_in_flight: bool = False


def _strip_html(text: str) -> str:
    if not text:
        return ""
    return unescape(_TAG_RE.sub(" ", text)).strip()


def _entry_published_ts(entry: object) -> float:
    """Best-effort extraction of an entry's publish time as unix epoch."""
    for attr in ("published_parsed", "updated_parsed", "created_parsed"):
        val = getattr(entry, attr, None)
        if val is not None:
            try:
                import calendar
                return float(calendar.timegm(val))
            except Exception:
                pass
    return 0.0


async def _fetch_one_feed(
    session: aiohttp.ClientSession, feed_url: str,
) -> list[LiveNewsEntry]:
    import feedparser

    try:
        async with session.get(feed_url, allow_redirects=True) as resp:
            if resp.status != 200:
                return []
            body = await resp.read()
    except Exception as exc:
        log.debug("Live feed %s failed: %s", feed_url, exc)
        return []

    parsed = await asyncio.to_thread(feedparser.parse, body)
    out: list[LiveNewsEntry] = []
    for entry in parsed.entries[:_PER_FEED_LIMIT]:
        link = getattr(entry, "link", None)
        if not link or not isinstance(link, str):
            continue
        if not link.startswith(("http://", "https://")):
            continue
        title = _strip_html(getattr(entry, "title", "") or "")
        summary = _strip_html(getattr(entry, "summary", "") or "")
        if not title:
            continue
        out.append(LiveNewsEntry(
            url=link,
            title=title,
            summary=summary[:500],
            domain=urlparse(link).netloc.lower(),
            published_ts=_entry_published_ts(entry),
        ))
    return out


async def _refresh_cache(feeds: list[str]) -> None:
    """Fetch all feeds in parallel and replace the cache."""
    global _cache, _cache_loaded_at

    settings = get_settings()
    headers = {"User-Agent": settings.user_agent}
    timeout = aiohttp.ClientTimeout(total=_FETCH_TIMEOUT_SECS)

    import ssl as _ssl
    ssl_ctx = _ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = _ssl.CERT_NONE
    connector = aiohttp.TCPConnector(limit=16, ssl=ssl_ctx)

    sem = asyncio.Semaphore(8)

    async def _gated(session: aiohttp.ClientSession, f: str) -> list[LiveNewsEntry]:
        async with sem:
            return await _fetch_one_feed(session, f)

    async with aiohttp.ClientSession(
        connector=connector, timeout=timeout, headers=headers,
    ) as session:
        try:
            results = await asyncio.wait_for(
                asyncio.gather(
                    *[_gated(session, f) for f in feeds],
                    return_exceptions=False,
                ),
                timeout=_FETCH_TIMEOUT_SECS + 3,
            )
        except asyncio.TimeoutError:
            log.warning("Live news refresh timed out after %ds.", _FETCH_TIMEOUT_SECS)
            results = []

    seen: set[str] = set()
    merged: list[LiveNewsEntry] = []
    for batch in results:
        for e in batch:
            if e.url in seen:
                continue
            seen.add(e.url)
            merged.append(e)
            if len(merged) >= _MAX_ENTRIES:
                break
        if len(merged) >= _MAX_ENTRIES:
            break

    # Sort newest first so query filtering naturally favours recent items.
    merged.sort(key=lambda e: e.published_ts, reverse=True)

    _cache = merged
    _cache_loaded_at = time.time()
    log.info("Live news cache refreshed: %d entries from %d feeds.",
             len(merged), len(feeds))


async def _ensure_cache(feeds: list[str]) -> None:
    """Refresh the cache if it's empty or stale.

    Only one refresh runs at a time; concurrent searches share the result.
    """
    global _refresh_in_flight
    now = time.time()
    if _cache and (now - _cache_loaded_at) < _CACHE_TTL_SECS:
        return

    if _refresh_in_flight:
        # Another search is already refreshing; wait for it briefly.
        for _ in range(30):  # up to ~3 s
            await asyncio.sleep(0.1)
            if not _refresh_in_flight:
                break
        return

    async with _refresh_lock:
        # Double-check under the lock.
        if _cache and (time.time() - _cache_loaded_at) < _CACHE_TTL_SECS:
            return
        _refresh_in_flight = True
        try:
            await _refresh_cache(feeds)
        finally:
            _refresh_in_flight = False


def _score_entry(entry: LiveNewsEntry, tokens: list[str]) -> float:
    """Lightweight relevance score: token hits in title + summary, with a
    small recency boost.  Returns 0 if the entry doesn't match the query.
    """
    if not tokens:
        return 0.0
    title_lower = entry.title.lower()
    summary_lower = entry.summary.lower()
    title_hits = sum(1 for t in tokens if t in title_lower)
    summary_hits = sum(1 for t in tokens if t in summary_lower)
    if title_hits == 0 and summary_hits == 0:
        return 0.0
    score = title_hits * 5.0 + summary_hits * 1.0
    # Coverage bonus when more query terms match.
    matched = sum(
        1 for t in tokens if t in title_lower or t in summary_lower
    )
    score *= (matched / max(len(tokens), 1)) ** 0.5

    # Recency boost (up to ~1.5×) for items in the last 24h.
    if entry.published_ts > 0:
        age_hours = max((time.time() - entry.published_ts) / 3600, 0)
        if age_hours < 48:
            score *= 1.0 + 0.5 * max(0.0, 1.0 - age_hours / 48.0)
    return score


async def search_live_news(
    tokens: list[str],
    feeds: list[str],
    limit: int = 20,
) -> list[LiveNewsEntry]:
    """Return live news entries matching *tokens*, refreshing cache if needed.

    Safe to call from any search handler; never raises — returns ``[]`` if
    the cache can't be populated (e.g. no network).
    """
    if not tokens:
        return []

    try:
        await _ensure_cache(feeds)
    except Exception:
        log.exception("Live news cache refresh failed.")
        return []

    if not _cache:
        return []

    scored: list[tuple[float, LiveNewsEntry]] = []
    for entry in _cache:
        s = _score_entry(entry, tokens)
        if s > 0:
            scored.append((s, entry))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [e for _, e in scored[:limit]]


async def enqueue_for_crawl(urls: list[str]) -> None:
    """Enqueue article URLs we surfaced live, so the crawler picks them up
    next cycle and they end up fully indexed.  Best-effort; ignores errors.
    """
    if not urls:
        return
    try:
        from platysearch.database import get_db
        db = await get_db()
        try:
            for url in urls:
                domain = urlparse(url).netloc
                if not domain:
                    continue
                await db.execute(
                    "INSERT OR IGNORE INTO crawl_queue (url, domain, depth) "
                    "VALUES (?, ?, 0)",
                    (url, domain),
                )
            await db.commit()
        finally:
            await db.close()
    except Exception:
        log.debug("enqueue_for_crawl failed", exc_info=True)


def cache_stats() -> dict[str, object]:
    """Diagnostic stats for /admin or /debug pages."""
    return {
        "entries": len(_cache),
        "loaded_at": _cache_loaded_at,
        "age_secs": (time.time() - _cache_loaded_at) if _cache_loaded_at else None,
        "ttl_secs": _CACHE_TTL_SECS,
        "refresh_in_flight": _refresh_in_flight,
    }

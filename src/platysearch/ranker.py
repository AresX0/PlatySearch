"""Ranker — custom scoring algorithm combining relevance, authority, and AI penalty."""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from urllib.parse import urlparse

from platysearch.config import get_settings
from platysearch.database import get_db
from platysearch.indexer import tokenize

log = logging.getLogger(__name__)

# ── Domain ranking signals ───────────────────────────────────────────────────

# Domains that get a ranking boost (primary sources, official sites, quality content).
_PREFERRED_DOMAINS: set[str] = {
    "memory-alpha.fandom.com",
    "disney.fandom.com",
    "www.startrek.com",
    "www.starwars.com",
    "starwars.fandom.com",
    "theforce.net",
    "www.starwarsnewsnet.com",
    "www.disney.com",
    "www.marvel.com",
    "www.dc.com",
    "www.dndbeyond.com",
    "www.ex-astris-scientia.org",
    "www.stargatearchive.com",
    "www.thecompanion.app",
    "www.imdb.com",
    "www.rottentomatoes.com",
    "www.bcm.edu",
    "www.rice.edu",
    "news.rice.edu",
    "www.mit.edu",
    "news.mit.edu",
    "www.tcd.ie",
    "pubmed.ncbi.nlm.nih.gov",
    "www.sciencedaily.com",
    "www.newscientist.com",
    "www.scientificamerican.com",
    "www.the-scientist.com",
    "www.sciencenews.org",
    "www.science.org",
    "phys.org",
    "www.advancedsciencenews.com",
    "www.popsci.com",
    "apnews.com",
    "www.npr.org",
    "www.bbc.com",
    "www.bbc.co.uk",
    "www.reuters.com",
    "www.nature.com",
    "arstechnica.com",
    "www.wired.com",
    "www.darkreading.com",
    "thehackernews.com",
    "www.sans.org",
    "krebsonsecurity.com",
    "www.nationalgeographic.com",
    "www.loc.gov",
    "www.britannica.com",
    "www.forbes.com",
    "plato.stanford.edu",
    # Government / official
    "www.nasa.gov",
    "science.nasa.gov",
    "www.jpl.nasa.gov",
    "hubblesite.org",
    "webb.nasa.gov",
    "www.spacex.com",
    "www.fbi.gov",
    "www.uscourts.gov",
    "www.dps.texas.gov",
    "www.congress.gov",
    # News & media
    "www.nytimes.com",
    "variety.com",
    "www.hollywoodreporter.com",
    "deadline.com",
    "www.theguardian.com",
    "www.washingtonpost.com",
    "www.cnet.com",
    "www.theverge.com",
    "techcrunch.com",
    "www.nbcnews.com",
    "abcnews.go.com",
    "www.usatoday.com",
    "www.huffpost.com",
    "www.usnews.com",
    "www.chicagotribune.com",
    "www.chron.com",
    "abc13.com",
    "www.vox.com",
    "www.politico.com",
    "www.latimes.com",
    "www.texastribune.org",
    "www.miamiherald.com",
    "www.nbclosangeles.com",
    "www.afp.com",
    "www.chinadaily.com.cn",
    "www.scmp.com",
    "www.aljazeera.com",
    "www.euronews.com",
    "www.dw.com",
    "www.bloomberg.com",
    "www.cnbc.com",
    # Science & reference
    "www.space.com",
    "www.livescience.com",
    "www.astronomy.com",
    "www.smithsonianmag.com",
    "www.history.com",
    "www.ncbi.nlm.nih.gov",
    "www.jstor.org",
    "www.worldwildlife.org",
    "wwf.org.au",
    "animals.sandiegozoo.org",
    "sandiegozoowildlifealliance.org",
    "animalia.bio",
    "bie.ala.org.au",
    "platypusspot.org",
    "www.pbs.org",
    "isc.sans.edu",
    "onlinedegrees.sandiego.edu",
    # Education
    "www.stanford.edu",
    "www.harvard.edu",
    # Entertainment
    "www.metacritic.com",
    "tvtropes.org",
    # Social media — preferred
    "www.youtube.com",
    "youtube.com",
    "bsky.app",
    "www.threads.net",
    "threads.net",
    "platypusmatch.bsky.social",
    "anotherfrakkinpodcast.bsky.social",
}

# Domains demoted to mid-page results (around position 6-7).
_DEMOTED_DOMAINS: set[str] = {
    "en.wikipedia.org",
    "es.wikipedia.org",
    "fr.wikipedia.org",
    "de.wikipedia.org",
    "ja.wikipedia.org",
    "zh.wikipedia.org",
    "ru.wikipedia.org",
    "en.wikisource.org",
    "en.wiktionary.org",
}

# Social media sites pushed to second page and beyond.
_SOCIAL_MEDIA_DOMAINS: set[str] = {
    "www.facebook.com",
    "facebook.com",
    "www.instagram.com",
    "instagram.com",
    "twitter.com",
    "x.com",
    "www.tiktok.com",
    "tiktok.com",
    "www.reddit.com",
    "reddit.com",
    "www.pinterest.com",
    "pinterest.com",
    "www.snapchat.com",
    "www.linkedin.com",
    "linkedin.com",
    "mastodon.social",
    "www.tumblr.com",
    "tumblr.com",
}

_PREFERRED_BOOST = 1.5   # multiply score
_DEMOTED_PENALTY = 0.65  # multiply score (appear ~position 6-7)
_SOCIAL_MEDIA_PENALTY = 0.20  # push to second page


@dataclass
class SearchResult:
    page_id: int
    url: str
    title: str
    snippet: str
    score: float
    ai_score: float
    image_url: str = ""
    image_alt: str = ""


async def search(query: str, tab: str = "all", limit: int = 20) -> list[SearchResult]:
    """Run a search query and return ranked results."""
    if tab == "images":
        return await _image_search(query, limit)

    tokens = tokenize(query)
    if not tokens:
        return []

    db = await get_db()
    try:
        # Resolve term ids.
        placeholders = ",".join("?" for _ in tokens)
        term_rows = await db.execute_fetchall(
            f"SELECT id, term FROM terms WHERE term IN ({placeholders})", tokens
        )
        if not term_rows:
            return []

        term_ids = [row[0] for row in term_rows]
        matched_terms = {row[0]: row[1] for row in term_rows}

        # Gather TF-IDF scores for matching documents.
        ph2 = ",".join("?" for _ in term_ids)
        posting_rows = await db.execute_fetchall(
            f"""SELECT page_id, SUM(tf) AS relevance
                FROM postings
                WHERE term_id IN ({ph2})
                GROUP BY page_id
                ORDER BY relevance DESC
                LIMIT 200""",
            term_ids,
        )
        if not posting_rows:
            return []

        page_ids = [r[0] for r in posting_rows]
        relevance_map = {r[0]: r[1] for r in posting_rows}

        # Fetch page metadata and scores.
        ph3 = ",".join("?" for _ in page_ids)

        # Tab-based content-type filter.
        type_filter = ""
        query_params = list(page_ids)
        if tab == "news":
            type_filter = "AND p.content_type = ?"
            query_params.append("news")
        elif tab == "images":
            type_filter = "AND p.content_type = ?"
            query_params.append("image")
        elif tab == "video":
            type_filter = "AND p.content_type = ?"
            query_params.append("video")

        page_rows = await db.execute_fetchall(
            f"""SELECT p.id, p.url, p.title, p.body,
                       COALESCE(s.inbound_links, 0),
                       COALESCE(s.domain_diversity, 0),
                       COALESCE(s.content_length, 0),
                       COALESCE(s.ai_score, 0.0),
                       COALESCE(s.quality_score, 0.0)
                FROM pages p
                LEFT JOIN page_scores s ON s.page_id = p.id
                WHERE p.id IN ({ph3}) {type_filter}""",
            query_params,
        )

        settings = get_settings()
        results: list[SearchResult] = []

        for row in page_rows:
            (page_id, url, title, body, inbound, domain_div,
             content_length, ai_score, quality_score) = row

            relevance = relevance_map.get(page_id, 0.0)

            # ── Authority signal (log-scaled inbound links) ──
            authority = math.log1p(inbound) * (1 + 0.5 * math.log1p(domain_div))

            # ── Content quality signal ──
            # Reward pages with a decent amount of text (diminishing returns).
            length_factor = min(math.log1p(content_length) / 10, 1.0)

            # ── Combine signals ──
            raw_score = (
                relevance * 10.0
                + authority * 2.0
                + length_factor * 1.0
                + quality_score * 1.0
            )

            # ── AI-content penalty ──
            # ai_score in [0, 1] — higher means more likely AI.
            ai_multiplier = 1.0 - ai_score * (1.0 - settings.ai_penalty)
            final_score = raw_score * ai_multiplier

            # ── Domain preference signal ──
            domain = urlparse(url).netloc.lower()
            if domain in _PREFERRED_DOMAINS:
                final_score *= _PREFERRED_BOOST
            elif domain in _SOCIAL_MEDIA_DOMAINS:
                final_score *= _SOCIAL_MEDIA_PENALTY
            elif domain in _DEMOTED_DOMAINS:
                final_score *= _DEMOTED_PENALTY

            snippet = _make_snippet(body or "", tokens)

            results.append(SearchResult(
                page_id=page_id,
                url=url,
                title=title or url,
                snippet=snippet,
                score=final_score,
                ai_score=ai_score,
            ))

        results.sort(key=lambda r: r.score, reverse=True)
        return results[:limit]
    finally:
        await db.close()


async def _image_search(query: str, limit: int = 40) -> list[SearchResult]:
    """Find images from pages matching the query."""
    tokens = tokenize(query)
    if not tokens:
        return []

    db = await get_db()
    try:
        placeholders = ",".join("?" for _ in tokens)
        term_rows = await db.execute_fetchall(
            f"SELECT id FROM terms WHERE term IN ({placeholders})", tokens
        )
        if not term_rows:
            return []

        term_ids = [row[0] for row in term_rows]
        ph2 = ",".join("?" for _ in term_ids)

        # Get the most relevant pages that have images.
        rows = await db.execute_fetchall(
            f"""SELECT po.page_id, SUM(po.tf) AS relevance
                FROM postings po
                WHERE po.term_id IN ({ph2})
                  AND EXISTS (SELECT 1 FROM page_images pi WHERE pi.page_id = po.page_id)
                GROUP BY po.page_id
                ORDER BY relevance DESC
                LIMIT 200""",
            term_ids,
        )
        if not rows:
            return []

        page_ids = [r[0] for r in rows]
        relevance_map = {r[0]: r[1] for r in rows}
        ph3 = ",".join("?" for _ in page_ids)

        # Fetch images with their page info.
        image_rows = await db.execute_fetchall(
            f"""SELECT pi.src_url, pi.alt_text, p.id, p.url, p.title,
                       COALESCE(s.ai_score, 0.0)
                FROM page_images pi
                JOIN pages p ON p.id = pi.page_id
                LEFT JOIN page_scores s ON s.page_id = p.id
                WHERE pi.page_id IN ({ph3})
                ORDER BY pi.id""",
            page_ids,
        )

        # Build results — one per image, scored by the page relevance.
        # Use the alt text for matching bonus.
        query_lower = query.lower()
        results: list[SearchResult] = []
        seen_urls: set[str] = set()

        for img_src, alt_text, page_id, page_url, title, ai_score in image_rows:
            if img_src in seen_urls:
                continue
            seen_urls.add(img_src)

            relevance = relevance_map.get(page_id, 0.0)
            score = relevance * 10.0

            # Boost images whose alt text matches query.
            alt_lower = (alt_text or "").lower()
            if any(t in alt_lower for t in tokens):
                score *= 1.5

            # Domain preference.
            domain = urlparse(page_url).netloc.lower()
            if domain in _PREFERRED_DOMAINS:
                score *= _PREFERRED_BOOST
            elif domain in _SOCIAL_MEDIA_DOMAINS:
                score *= _SOCIAL_MEDIA_PENALTY
            elif domain in _DEMOTED_DOMAINS:
                score *= _DEMOTED_PENALTY

            results.append(SearchResult(
                page_id=page_id,
                url=page_url,
                title=title or page_url,
                snippet=alt_text or "",
                score=score,
                ai_score=ai_score,
                image_url=img_src,
                image_alt=alt_text or title or "",
            ))

        results.sort(key=lambda r: r.score, reverse=True)
        return results[:limit]
    finally:
        await db.close()


def _make_snippet(body: str, query_tokens: list[str], length: int = 200) -> str:
    """Extract the most relevant snippet from the body text."""
    lower = body.lower()
    best_pos = 0
    best_count = 0

    # Slide a window across the text to find the densest cluster of query terms.
    window = length * 2
    for i in range(0, max(1, len(body) - window), window // 4):
        chunk = lower[i : i + window]
        count = sum(chunk.count(t) for t in query_tokens)
        if count > best_count:
            best_count = count
            best_pos = i

    start = max(0, best_pos - 20)
    end = start + length
    snippet = body[start:end].strip()
    if start > 0:
        snippet = "…" + snippet
    if end < len(body):
        snippet += "…"
    return snippet

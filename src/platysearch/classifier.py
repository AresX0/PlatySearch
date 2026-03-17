"""Content-type classifier — tags pages as page/news/image/video using URL and HTML heuristics."""

from __future__ import annotations

import re
from urllib.parse import urlparse

# ── Domain / path patterns ───────────────────────────────────────────────────

_NEWS_DOMAINS = re.compile(
    r"(news|bbc|cnn|reuters|nytimes|theguardian|washingtonpost|apnews|npr\.org"
    r"|aljazeera|nbcnews|cbsnews|foxnews|abcnews|usatoday|dailymail"
    r"|politico|thehill|huffpost|bloomberg|techcrunch|arstechnica"
    r"|theverge|wired\.com|engadget)",
    re.IGNORECASE,
)

_NEWS_PATH = re.compile(
    r"/(news|article|story|press|blog|post|breaking|opinion|editorial)/",
    re.IGNORECASE,
)

_IMAGE_EXTENSIONS = re.compile(r"\.(jpe?g|png|gif|webp|svg|bmp|ico|tiff?)(\?|$)", re.IGNORECASE)

_IMAGE_DOMAINS = re.compile(
    r"(flickr|imgur|unsplash|pexels|pixabay|500px|deviantart|instagram)",
    re.IGNORECASE,
)

_VIDEO_DOMAINS = re.compile(
    r"(youtube|youtu\.be|vimeo|dailymotion|twitch|tiktok|rumble|bitchute|odysee)",
    re.IGNORECASE,
)

_VIDEO_PATH = re.compile(r"/(watch|video|embed|clip|stream)/", re.IGNORECASE)


def classify_page(url: str, title: str, html: str) -> str:
    """Return one of: 'news', 'image', 'video', 'page'."""
    parsed = urlparse(url)
    domain = parsed.netloc.lower()
    path = parsed.path.lower()

    # ── Video ──
    if _VIDEO_DOMAINS.search(domain) or _VIDEO_PATH.search(path):
        return "video"

    # ── Image ──
    if _IMAGE_EXTENSIONS.search(path) or _IMAGE_DOMAINS.search(domain):
        return "image"

    # ── News ──
    if _NEWS_DOMAINS.search(domain) or _NEWS_PATH.search(path):
        return "news"

    # Check HTML meta tags for more signals.
    html_lower = html[:5000].lower() if html else ""
    if 'og:type" content="article' in html_lower or 'og:type" content="news' in html_lower:
        return "news"
    if 'og:type" content="video' in html_lower:
        return "video"
    if 'og:type" content="image' in html_lower:
        return "image"

    return "page"

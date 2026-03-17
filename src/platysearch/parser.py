"""HTML parser — extracts text, metadata, and links from raw HTML."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup


@dataclass
class ParsedPage:
    url: str
    title: str
    body: str
    links: list[str] = field(default_factory=list)
    content_hash: str = ""
    meta_description: str = ""


# Tags whose text content is not useful for indexing.
_STRIP_TAGS = {"script", "style", "noscript", "svg", "nav", "footer", "header", "aside", "form"}


def parse_html(url: str, html: str) -> ParsedPage:
    """Parse raw HTML and return structured content."""
    soup = BeautifulSoup(html, "lxml")

    # ── Title ──
    title_tag = soup.find("title")
    title = title_tag.get_text(strip=True) if title_tag else ""

    # ── Meta description ──
    meta = soup.find("meta", attrs={"name": "description"})
    meta_desc = meta.get("content", "") if meta else ""

    # ── Strip non-content tags ──
    for tag in soup.find_all(_STRIP_TAGS):
        tag.decompose()

    # ── Body text ──
    # Prefer <main> or <article> if available, else fall back to <body>.
    main = soup.find("main") or soup.find("article") or soup.find("body")
    body = _clean_text(main.get_text(separator=" ") if main else "")

    # ── Links ──
    base_url = url
    base_tag = soup.find("base", href=True)
    if base_tag:
        base_url = str(base_tag["href"])

    links: list[str] = []
    for a in soup.find_all("a", href=True):
        href = str(a["href"]).strip()
        if href.startswith(("javascript:", "mailto:", "tel:", "#")):
            continue
        absolute = urljoin(base_url, href)
        # Only keep http(s) links.
        parsed = urlparse(absolute)
        if parsed.scheme in ("http", "https"):
            # Strip fragment.
            clean = parsed._replace(fragment="").geturl()
            links.append(clean)

    content_hash = hashlib.sha256(body.encode()).hexdigest()

    return ParsedPage(
        url=url,
        title=title,
        body=body,
        links=links,
        content_hash=content_hash,
        meta_description=meta_desc,
    )


_MULTI_SPACE = re.compile(r"\s+")


def _clean_text(text: str) -> str:
    """Collapse whitespace and strip."""
    return _MULTI_SPACE.sub(" ", text).strip()

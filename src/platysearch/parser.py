"""HTML parser — extracts text, metadata, and links from raw HTML."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup


@dataclass
class ImageInfo:
    src: str
    alt: str = ""
    width: int = 0
    height: int = 0


@dataclass
class ParsedPage:
    url: str
    title: str
    body: str
    links: list[str] = field(default_factory=list)
    images: list[ImageInfo] = field(default_factory=list)
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

    # ── Base URL ──
    base_url = url
    base_tag = soup.find("base", href=True)
    if base_tag:
        base_url = str(base_tag["href"])

    # ── Images (extract before stripping tags) ──
    images = _extract_images(soup, base_url)

    # ── Links (extract BEFORE stripping tags so we capture nav/sidebar/footer links) ──

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

    # ── Strip non-content tags ──
    for tag in soup.find_all(_STRIP_TAGS):
        tag.decompose()

    # ── Body text ──
    # Prefer <main> or <article> if available, else fall back to <body>.
    main = soup.find("main") or soup.find("article") or soup.find("body")
    body = _clean_text(main.get_text(separator=" ") if main else "")

    content_hash = hashlib.sha256(body.encode()).hexdigest()

    return ParsedPage(
        url=url,
        title=title,
        body=body,
        links=links,
        images=images,
        content_hash=content_hash,
        meta_description=meta_desc,
    )


_MULTI_SPACE = re.compile(r"\s+")

_MIN_IMAGE_DIM = 80  # ignore tiny icons / spacers
_IMAGE_EXT = re.compile(r"\.(jpe?g|png|gif|webp|svg)(\?|$)", re.IGNORECASE)


def _extract_images(soup: BeautifulSoup, base_url: str) -> list[ImageInfo]:
    """Extract meaningful images from the page (skip tiny icons, data URIs)."""
    seen: set[str] = set()
    images: list[ImageInfo] = []

    # og:image is usually a good hero image
    og = soup.find("meta", property="og:image")
    if og and og.get("content"):
        src = _resolve_image_src(str(og["content"]), base_url)
        if src and src not in seen:
            seen.add(src)
            images.append(ImageInfo(src=src, alt=""))

    for img in soup.find_all("img", src=True):
        raw_src = str(img["src"]).strip()
        if not raw_src or raw_src.startswith("data:"):
            continue
        src = _resolve_image_src(raw_src, base_url)
        if not src or src in seen:
            continue

        # Skip tiny images (icons, tracking pixels).
        w = _int_attr(img.get("width"))
        h = _int_attr(img.get("height"))
        if (w and w < _MIN_IMAGE_DIM) or (h and h < _MIN_IMAGE_DIM):
            continue

        alt = str(img.get("alt", "")).strip()
        seen.add(src)
        images.append(ImageInfo(src=src, alt=alt, width=w, height=h))

    return images


def _resolve_image_src(raw: str, base_url: str) -> str | None:
    """Turn a raw src attribute into an absolute https/http URL, or None."""
    absolute = urljoin(base_url, raw)
    parsed = urlparse(absolute)
    if parsed.scheme not in ("http", "https"):
        return None
    return parsed._replace(fragment="").geturl()


def _int_attr(v: str | None) -> int:
    """Parse an HTML integer attribute, returning 0 on failure."""
    if not v:
        return 0
    try:
        return int(re.sub(r"[^\d]", "", str(v)))
    except (ValueError, TypeError):
        return 0


def _clean_text(text: str) -> str:
    """Collapse whitespace and strip."""
    return _MULTI_SPACE.sub(" ", text).strip()

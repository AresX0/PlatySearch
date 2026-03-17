"""Indexer — builds an inverted index with TF-IDF term weights."""

from __future__ import annotations

import logging
import math
import re
from collections import Counter

import nltk
from nltk.corpus import stopwords
from nltk.tokenize import word_tokenize

from platysearch.database import get_db

log = logging.getLogger(__name__)

_STOP_WORDS: set[str] | None = None


def _get_stop_words() -> set[str]:
    global _STOP_WORDS
    if _STOP_WORDS is None:
        try:
            _STOP_WORDS = set(stopwords.words("english"))
        except LookupError:
            nltk.download("stopwords", quiet=True)
            _STOP_WORDS = set(stopwords.words("english"))
    return _STOP_WORDS


_TOKEN_RE = re.compile(r"[a-z0-9]+")


def tokenize(text: str) -> list[str]:
    """Lowercase, strip non-alphanumeric, remove stop words."""
    stops = _get_stop_words()
    try:
        tokens = word_tokenize(text.lower())
    except LookupError:
        nltk.download("punkt_tab", quiet=True)
        tokens = word_tokenize(text.lower())
    return [t for t in tokens if _TOKEN_RE.fullmatch(t) and t not in stops]


async def index_all_pages() -> int:
    """(Re-)index every page currently stored. Returns number of pages indexed."""
    db = await get_db()
    try:
        rows = await db.execute_fetchall("SELECT id, title, body FROM pages")
        if not rows:
            log.info("No pages to index.")
            return 0

        total_docs = len(rows)
        log.info("Indexing %d pages…", total_docs)

        # Compute term frequencies per document.
        doc_term_freqs: dict[int, Counter[str]] = {}
        doc_freq: Counter[str] = Counter()

        for page_id, title, body in rows:
            text = f"{title or ''} {title or ''} {body or ''}"  # Title weighted 2×.
            tokens = tokenize(text)
            tf = Counter(tokens)
            doc_term_freqs[page_id] = tf
            doc_freq.update(tf.keys())

        # Clear old postings.
        await db.execute("DELETE FROM postings")
        await db.execute("DELETE FROM terms")

        # Insert terms.
        all_terms = sorted(doc_freq.keys())
        await db.executemany(
            "INSERT INTO terms (term) VALUES (?)", [(t,) for t in all_terms]
        )
        await db.commit()

        # Build term→id mapping.
        term_rows = await db.execute_fetchall("SELECT id, term FROM terms")
        term_id_map = {term: tid for tid, term in term_rows}

        # Insert postings with TF-IDF.
        postings = []
        for page_id, tf_counter in doc_term_freqs.items():
            total_terms = sum(tf_counter.values()) or 1
            for term, count in tf_counter.items():
                tf = count / total_terms
                idf = math.log((1 + total_docs) / (1 + doc_freq[term])) + 1
                tfidf = tf * idf
                positions = ""  # Could store positions later for phrase matching.
                postings.append((term_id_map[term], page_id, tfidf, positions))

        await db.executemany(
            "INSERT INTO postings (term_id, page_id, tf, positions) VALUES (?, ?, ?, ?)",
            postings,
        )
        await db.commit()
        log.info("Indexed %d pages with %d unique terms.", total_docs, len(all_terms))
        return total_docs
    finally:
        await db.close()


async def compute_link_scores() -> None:
    """Compute inbound link count and domain diversity per page."""
    db = await get_db()
    try:
        # Inbound link counts.
        rows = await db.execute_fetchall(
            """
            SELECT p.id,
                   COUNT(l.id) AS inbound,
                   COUNT(DISTINCT src.domain) AS domain_div,
                   LENGTH(p.body) AS clen
            FROM pages p
            LEFT JOIN links l ON l.target_url = p.url
            LEFT JOIN pages src ON src.id = l.source_id
            GROUP BY p.id
            """
        )
        await db.execute("DELETE FROM page_scores")
        for page_id, inbound, domain_div, clen in rows:
            await db.execute(
                """INSERT OR REPLACE INTO page_scores
                   (page_id, inbound_links, domain_diversity, content_length)
                   VALUES (?, ?, ?, ?)""",
                (page_id, inbound, domain_div, clen or 0),
            )
        await db.commit()
        log.info("Computed link scores for %d pages.", len(rows))
    finally:
        await db.close()

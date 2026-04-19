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
    """(Re-)index every page currently stored. Returns number of pages indexed.

    Uses batched processing to stay within tight memory limits (e.g. 1.75 GB).
    """
    _BATCH = 500
    db = await get_db()
    try:
        cursor = await db.execute("SELECT COUNT(*) FROM pages")
        row = await cursor.fetchone()
        total_docs = row[0] if row else 0
        if total_docs == 0:
            log.info("No pages to index.")
            return 0

        log.info("Indexing %d pages (batched)…", total_docs)

        # ── Pass 1: compute document frequencies (streaming) ─────────
        doc_freq: Counter[str] = Counter()
        last_id = 0
        while True:
            rows = await db.execute_fetchall(
                "SELECT id, title, body FROM pages WHERE id > ? ORDER BY id LIMIT ?",
                (last_id, _BATCH),
            )
            if not rows:
                break
            for page_id, title, body in rows:
                text = f"{title or ''} {title or ''} {body or ''}"
                tokens = tokenize(text)
                doc_freq.update(set(tokens))  # set → count each term once per doc
                last_id = page_id

        # ── Rebuild terms table ──────────────────────────────────────
        await db.execute("DELETE FROM postings")
        await db.execute("DELETE FROM terms")
        await db.commit()

        all_terms = sorted(doc_freq.keys())
        # Insert terms in chunks to limit memory.
        for i in range(0, len(all_terms), 5000):
            await db.executemany(
                "INSERT INTO terms (term) VALUES (?)",
                [(t,) for t in all_terms[i : i + 5000]],
            )
        await db.commit()
        del all_terms  # free memory

        # Build term→id mapping (streamed).
        term_id_map: dict[str, int] = {}
        last_tid = 0
        while True:
            trows = await db.execute_fetchall(
                "SELECT id, term FROM terms WHERE id > ? ORDER BY id LIMIT ?",
                (last_tid, 10000),
            )
            if not trows:
                break
            for tid, term in trows:
                term_id_map[term] = tid
                last_tid = tid

        # ── Pass 2: compute TF-IDF and insert postings in batches ────
        last_id = 0
        indexed = 0
        while True:
            rows = await db.execute_fetchall(
                "SELECT id, title, body FROM pages WHERE id > ? ORDER BY id LIMIT ?",
                (last_id, _BATCH),
            )
            if not rows:
                break

            postings_batch: list[tuple] = []
            for page_id, title, body in rows:
                text = f"{title or ''} {title or ''} {body or ''}"
                tokens = tokenize(text)
                tf_counter = Counter(tokens)
                total_terms = sum(tf_counter.values()) or 1
                for term, count in tf_counter.items():
                    tf = count / total_terms
                    idf = math.log((1 + total_docs) / (1 + doc_freq[term])) + 1
                    tfidf = tf * idf
                    postings_batch.append((term_id_map[term], page_id, tfidf, ""))
                indexed += 1
                last_id = page_id

            if postings_batch:
                await db.executemany(
                    "INSERT INTO postings (term_id, page_id, tf, positions) VALUES (?, ?, ?, ?)",
                    postings_batch,
                )
                await db.commit()

            if indexed % 5000 == 0:
                log.info("Indexed %d / %d pages…", indexed, total_docs)

        log.info("Indexed %d pages with %d unique terms.", indexed, len(term_id_map))
        return indexed
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

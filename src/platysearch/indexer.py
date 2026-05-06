"""Indexer — builds an inverted index with TF-IDF term weights."""

from __future__ import annotations

import logging
import math
import re
from collections import Counter

import nltk
from nltk.corpus import stopwords
from nltk.tokenize import word_tokenize

from platysearch.database import get_db, get_meta, set_meta

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


async def index_all_pages(force: bool = False) -> int:
    """(Re-)index every page currently stored. Returns number of pages indexed.

    Uses batched processing to stay within tight memory limits.

    Skips work entirely when no new pages have been added since the last
    successful index (tracked via the ``meta`` table). Pass ``force=True``
    to bypass the skip and force a full rebuild.
    """
    # ── Fast skip: no new pages since last index ─────────────────────
    if not force:
        db_chk = await get_db()
        try:
            cur = await db_chk.execute("SELECT COALESCE(MAX(id), 0) FROM pages")
            row = await cur.fetchone()
            current_max = row[0] if row else 0
            cur = await db_chk.execute("SELECT COUNT(*) FROM pages")
            row = await cur.fetchone()
            current_count = row[0] if row else 0
        finally:
            await db_chk.close()
        last_max_str = await get_meta("last_indexed_max_page_id")
        last_max = int(last_max_str) if last_max_str and last_max_str.isdigit() else 0
        if current_max > 0 and current_max == last_max:
            log.info(
                "Index up-to-date (max page id %d unchanged) \u2014 skipping rebuild.",
                current_max,
            )
            return current_count

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
        # Flush postings every _POSTINGS_FLUSH rows to keep memory bounded
        # regardless of vocabulary size or document length.
        _POSTINGS_FLUSH = 50_000
        last_id = 0
        indexed = 0
        postings_batch: list[tuple] = []
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
                tf_counter = Counter(tokens)
                total_terms = sum(tf_counter.values()) or 1
                for term, count in tf_counter.items():
                    tf = count / total_terms
                    idf = math.log((1 + total_docs) / (1 + doc_freq[term])) + 1
                    tfidf = tf * idf
                    postings_batch.append((term_id_map[term], page_id, tfidf, ""))
                indexed += 1
                last_id = page_id

                if len(postings_batch) >= _POSTINGS_FLUSH:
                    await db.executemany(
                        "INSERT INTO postings (term_id, page_id, tf, positions) VALUES (?, ?, ?, ?)",
                        postings_batch,
                    )
                    await db.commit()
                    postings_batch.clear()

            if indexed % 5000 == 0:
                log.info("Indexed %d / %d pages…", indexed, total_docs)

        if postings_batch:
            await db.executemany(
                "INSERT INTO postings (term_id, page_id, tf, positions) VALUES (?, ?, ?, ?)",
                postings_batch,
            )
            await db.commit()
            postings_batch.clear()

        log.info("Indexed %d pages with %d unique terms.", indexed, len(term_id_map))
        # Record watermark so subsequent calls can skip when nothing changed.
        cur = await db.execute("SELECT COALESCE(MAX(id), 0) FROM pages")
        row = await cur.fetchone()
        max_id = row[0] if row else 0
        await set_meta("last_indexed_max_page_id", str(max_id))
        return indexed
    finally:
        await db.close()


async def compute_link_scores() -> None:
    """Compute inbound link count and domain diversity per page.

    Pure-SQL streaming implementation — never materialises all rows in Python,
    so memory usage stays flat even with millions of pages/links.
    """
    db = await get_db()
    try:
        await db.execute("DELETE FROM page_scores")
        await db.commit()
        # Single INSERT...SELECT that SQLite streams internally.
        await db.execute(
            """
            INSERT INTO page_scores
                (page_id, inbound_links, domain_diversity, content_length)
            SELECT p.id,
                   COUNT(l.id) AS inbound,
                   COUNT(DISTINCT src.domain) AS domain_div,
                   COALESCE(LENGTH(p.body), 0) AS clen
            FROM pages p
            LEFT JOIN links l ON l.target_url = p.url
            LEFT JOIN pages src ON src.id = l.source_id
            GROUP BY p.id
            """
        )
        await db.commit()
        cur = await db.execute("SELECT COUNT(*) FROM page_scores")
        row = await cur.fetchone()
        log.info("Computed link scores for %d pages.", row[0] if row else 0)
    finally:
        await db.close()

"""AI content detector — scores text for likelihood of being AI-generated.

Uses purely statistical heuristics (no ML model required):

1. **Burstiness** — Human writing varies in sentence length; AI tends to be uniform.
2. **Vocabulary richness** — Type-token ratio; AI text often reuses the same words.
3. **Repetition patterns** — Repeated n-grams are common in AI output.
4. **Sentence-start diversity** — AI frequently starts sentences the same way.
5. **Punctuation diversity** — Humans use more varied punctuation.

Each signal produces a partial score in [0, 1].  The final AI-likelihood score
is a weighted average of all signals, also in [0, 1].
"""

from __future__ import annotations

import logging
import math
import re
import string
from collections import Counter

from platysearch.database import get_db

log = logging.getLogger(__name__)

# ── Sentence splitter ────────────────────────────────────────────────────────

_SENT_RE = re.compile(r"(?<=[.!?])\s+")


def _split_sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENT_RE.split(text) if s.strip()]


# ── Signal functions ─────────────────────────────────────────────────────────


def _burstiness(sentences: list[str]) -> float:
    """Low burstiness (uniform sentence length) → higher AI score."""
    if len(sentences) < 3:
        return 0.0
    lengths = [len(s.split()) for s in sentences]
    mean = sum(lengths) / len(lengths)
    if mean == 0:
        return 0.0
    variance = sum((l - mean) ** 2 for l in lengths) / len(lengths)
    std = math.sqrt(variance)
    cv = std / mean  # coefficient of variation

    # Humans typically cv > 0.5; AI < 0.35.
    if cv >= 0.6:
        return 0.0  # Very bursty → human.
    if cv <= 0.2:
        return 1.0  # Very uniform → AI.
    return 1.0 - (cv - 0.2) / 0.4


def _vocabulary_richness(words: list[str]) -> float:
    """Low type-token ratio → higher AI score."""
    if len(words) < 20:
        return 0.0
    # Use a sample to normalise for length.
    sample = words[:500]
    types = len(set(sample))
    ttr = types / len(sample)

    # Human text ttr ≈ 0.55–0.75; AI ≈ 0.35–0.50.
    if ttr >= 0.65:
        return 0.0
    if ttr <= 0.35:
        return 1.0
    return 1.0 - (ttr - 0.35) / 0.30


def _repetition_score(words: list[str], n: int = 3) -> float:
    """High proportion of repeated n-grams → higher AI score."""
    if len(words) < n + 5:
        return 0.0
    ngrams = [tuple(words[i : i + n]) for i in range(len(words) - n + 1)]
    counts = Counter(ngrams)
    repeated = sum(c - 1 for c in counts.values() if c > 1)
    ratio = repeated / len(ngrams)

    if ratio <= 0.02:
        return 0.0
    if ratio >= 0.15:
        return 1.0
    return (ratio - 0.02) / 0.13


def _sentence_start_diversity(sentences: list[str]) -> float:
    """If most sentences start with the same word → higher AI score."""
    if len(sentences) < 5:
        return 0.0
    starters = []
    for s in sentences:
        parts = s.split()
        if parts:
            starters.append(parts[0].lower())
    unique_ratio = len(set(starters)) / len(starters) if starters else 1.0

    if unique_ratio >= 0.8:
        return 0.0
    if unique_ratio <= 0.3:
        return 1.0
    return 1.0 - (unique_ratio - 0.3) / 0.5


def _punctuation_diversity(text: str) -> float:
    """AI text relies heavily on periods; humans use commas, dashes, semicolons more."""
    punct_counts = Counter(c for c in text if c in string.punctuation)
    total = sum(punct_counts.values())
    if total < 10:
        return 0.0
    unique = len(punct_counts)
    # Normalise: humans typically ≥ 8 unique punctuation types per 500 punct chars.
    ratio = unique / min(total, 500) * 100
    if ratio >= 3.0:
        return 0.0
    if ratio <= 1.0:
        return 1.0
    return 1.0 - (ratio - 1.0) / 2.0


# ── Public API ───────────────────────────────────────────────────────────────

# Weights for each signal.
_WEIGHTS = {
    "burstiness": 0.30,
    "vocabulary": 0.20,
    "repetition": 0.20,
    "sent_start": 0.15,
    "punctuation": 0.15,
}


def compute_ai_score(text: str) -> float:
    """Return a score in [0, 1] indicating AI-generation likelihood.

    0 = probably human, 1 = probably AI.
    """
    sentences = _split_sentences(text)
    words = text.lower().split()

    signals = {
        "burstiness": _burstiness(sentences),
        "vocabulary": _vocabulary_richness(words),
        "repetition": _repetition_score(words),
        "sent_start": _sentence_start_diversity(sentences),
        "punctuation": _punctuation_diversity(text),
    }

    score = sum(signals[k] * _WEIGHTS[k] for k in _WEIGHTS)
    return round(min(max(score, 0.0), 1.0), 4)


async def score_all_pages() -> int:
    """Compute and store AI scores for every page in the database."""
    db = await get_db()
    try:
        rows = await db.execute_fetchall("SELECT id, body FROM pages WHERE body IS NOT NULL")
        count = 0
        for page_id, body in rows:
            if not body or len(body) < 100:
                continue
            ai = compute_ai_score(body)
            await db.execute(
                """INSERT INTO page_scores (page_id, ai_score)
                   VALUES (?, ?)
                   ON CONFLICT(page_id)
                   DO UPDATE SET ai_score = excluded.ai_score""",
                (page_id, ai),
            )
            count += 1
        await db.commit()
        log.info("Scored %d pages for AI content.", count)
        return count
    finally:
        await db.close()

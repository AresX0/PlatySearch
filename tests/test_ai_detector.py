"""Tests for the AI content detector."""

from platysearch.ai_detector import compute_ai_score


# Very uniform, repetitive text — should score higher (more AI-like).
_AI_LIKE_TEXT = (
    "This is a sentence about technology. "
    "This is a sentence about innovation. "
    "This is a sentence about progress. "
    "This is a sentence about development. "
    "This is a sentence about advancement. "
    "This is a sentence about improvement. "
    "This is a sentence about enhancement. "
    "This is a sentence about growth. "
    "This is a sentence about change. "
    "This is a sentence about the future. "
) * 5

# Varied, bursty text — should score lower (more human-like).
_HUMAN_LIKE_TEXT = (
    "I woke up late today — honestly, it was a mess. "
    "Coffee? Cold. Burnt the toast (again!). "
    "But then, surprisingly, the morning turned around completely. "
    "A friend called out of nowhere; we hadn't spoken in years! "
    "We talked for almost an hour about everything: travel plans, "
    "old memories from university, that ridiculous camping trip. "
    "Remember when Dave fell in the lake? Classic. "
    "Anyway... got to work, powered through a mountain of emails. "
    "Some days are just weird, you know? "
    "The kind where nothing goes to plan but somehow it all works out fine. "
    "Had spaghetti for dinner — homemade sauce this time, "
    "with basil from the garden. Not bad at all! "
    "Watched half a documentary about deep-sea creatures before crashing. "
    "Tomorrow: who knows. That's life, I suppose."
)


def test_ai_text_scores_higher():
    ai = compute_ai_score(_AI_LIKE_TEXT)
    human = compute_ai_score(_HUMAN_LIKE_TEXT)
    assert ai > human, f"AI score {ai} should be > human score {human}"


def test_scores_in_range():
    for text in [_AI_LIKE_TEXT, _HUMAN_LIKE_TEXT]:
        score = compute_ai_score(text)
        assert 0.0 <= score <= 1.0


def test_short_text_returns_low_score():
    score = compute_ai_score("Hello world.")
    assert score < 0.3

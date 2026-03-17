"""Tests for the indexer tokenizer."""

from platysearch.indexer import tokenize


def test_tokenize_removes_stopwords():
    tokens = tokenize("The quick brown fox jumps over the lazy dog")
    assert "the" not in tokens
    assert "over" not in tokens
    assert "quick" in tokens
    assert "fox" in tokens


def test_tokenize_lowercases():
    tokens = tokenize("Python JavaScript Rust")
    assert all(t == t.lower() for t in tokens)


def test_tokenize_strips_punctuation():
    tokens = tokenize("Hello, world! How's it going?")
    assert "," not in tokens
    assert "!" not in tokens

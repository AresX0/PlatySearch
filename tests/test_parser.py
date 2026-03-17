"""Tests for the HTML parser."""

from platysearch.parser import parse_html


def test_parse_extracts_title():
    html = "<html><head><title>Hello World</title></head><body><p>Content here</p></body></html>"
    result = parse_html("https://example.com", html)
    assert result.title == "Hello World"
    assert "Content here" in result.body


def test_parse_extracts_links():
    html = """
    <html><body>
        <a href="/about">About</a>
        <a href="https://other.com/page">Other</a>
        <a href="javascript:void(0)">Skip</a>
    </body></html>
    """
    result = parse_html("https://example.com/index.html", html)
    assert "https://example.com/about" in result.links
    assert "https://other.com/page" in result.links
    assert len(result.links) == 2  # javascript: link excluded


def test_parse_strips_scripts():
    html = """
    <html><body>
        <p>Real content</p>
        <script>var x = 1;</script>
        <style>.foo { }</style>
    </body></html>
    """
    result = parse_html("https://example.com", html)
    assert "var x" not in result.body
    assert ".foo" not in result.body
    assert "Real content" in result.body


def test_content_hash_is_stable():
    html = "<html><body>Same content</body></html>"
    r1 = parse_html("https://a.com", html)
    r2 = parse_html("https://b.com", html)
    assert r1.content_hash == r2.content_hash

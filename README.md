# PlatySearch

An independent search engine built from scratch, focused on **accuracy** and **filtering AI-generated content**.

## Architecture

```
┌─────────┐    ┌────────┐    ┌─────────┐    ┌───────────┐
│ Crawler │───▸│ Parser │───▸│ Indexer │───▸│  Ranker   │
└─────────┘    └────────┘    └─────────┘    └───────────┘
                                                  │
                                            ┌─────▼─────┐
                                            │ AI Filter │
                                            └───────────┘
                                                  │
                                            ┌─────▼─────┐
                                            │ Search API│
                                            └───────────┘
                                                  │
                                            ┌─────▼─────┐
                                            │  Web  UI  │
                                            └───────────┘
```

### Components

| Component | Purpose |
|-----------|---------|
| **Crawler** | Async web crawler that respects `robots.txt`, handles rate-limiting, and discovers new pages via link extraction. |
| **Parser** | Extracts clean text, metadata, and outbound links from raw HTML using BeautifulSoup. |
| **Indexer** | Builds an inverted index with TF-IDF term weighting stored in SQLite. |
| **AI Detector** | Scores content for AI-generation likelihood using statistical heuristics (burstiness, perplexity proxies, repetition patterns). |
| **Ranker** | Custom ranking algorithm combining relevance, source signals, freshness, and AI-content penalty. |
| **Search API** | FastAPI REST endpoint serving search queries. |
| **Web UI** | Lightweight Jinja2-based frontend. |

## Ranking Philosophy

PlatySearch does **not** wrap other search engines. Every result comes from pages the crawler has visited directly. The ranking algorithm rewards:

1. **Term relevance** — TF-IDF similarity between query and document.
2. **Content quality** — Longer, well-structured content with diverse vocabulary.
3. **Source authority** — Inbound link count, domain diversity of backlinks.
4. **Freshness** — Recency of content and last-crawl date.
5. **Human-written penalty** — AI-generated content receives a negative score modifier.

## Quick Start

```bash
# Install
pip install -e ".[dev]"

# Download NLTK data (one-time)
python -c "import nltk; nltk.download('punkt_tab'); nltk.download('stopwords')"

# Initialize the database
platysearch init

# Start crawling from seed URLs
platysearch crawl --seeds https://example.com

# Start the search server
platysearch serve
```

Then open http://localhost:8000 in your browser.

## Configuration

Copy `.env.example` to `.env` and adjust settings, or set environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `PLATY_DB_PATH` | `data/platysearch.db` | SQLite database path |
| `PLATY_CRAWL_DELAY` | `1.0` | Seconds between requests to the same domain |
| `PLATY_MAX_PAGES` | `10000` | Max pages to crawl per session |
| `PLATY_AI_PENALTY` | `0.6` | Score multiplier for AI-detected content (0–1) |
| `PLATY_HOST` | `0.0.0.0` | Server bind address |
| `PLATY_PORT` | `8000` | Server port |

## License

MIT

# PlatySearch — Copilot Instructions

## Project Goals

PlatySearch is an **independent search engine** — it does not wrap, proxy, or depend on any existing search engine (Google, Bing, etc.). Every result is sourced from pages crawled directly by our own crawler.

### Core Principles

1. **Accuracy over volume** — Ranking prioritises relevance and content quality over sheer page count. A smaller index of high-quality, accurate results is better than a large index of noise.

2. **Filter AI-generated content** — AI-generated text is detected using statistical heuristics (burstiness, vocabulary richness, n-gram repetition, sentence-start uniformity, punctuation diversity) and penalised in ranking. The goal is to surface human-written, original content.

3. **Full independence** — No external search APIs, no result scraping, no third-party ranking signals. The crawler, indexer, ranker, and AI detector are all built from scratch.

4. **Transparency** — Search results show an AI-likelihood badge so users can see how content was classified.

## Architecture

- **Python 3.11+**, FastAPI, SQLite, aiohttp
- **Crawler** — async, respects robots.txt, rate-limited per domain
- **Parser** — BeautifulSoup/lxml, extracts clean text from `<main>`/`<article>`
- **Indexer** — inverted index with TF-IDF weighting (NLTK tokenisation)
- **AI Detector** — 5 weighted statistical signals, no ML model dependency
- **Ranker** — combines relevance, authority (inbound links), content quality, and AI penalty
- **Web UI** — Jinja2 templates, dark theme, logo at `/static/logo.png`

## Deployment

- Containerised via Docker
- Target platform: **Azure App Service** (Linux container)
- Custom domain: subdomain of **platysoft.com** (e.g. `search.platysoft.com`)
- Infrastructure defined in Bicep (`infra/`)
- **Do NOT rebuild/push the seed database** (`data/platysearch.db.gz`) during routine deploys. The production DB is persistent on Azure (`/home/data/`), updated nightly by the scheduler, and survives container restarts. Only include a new seed DB if explicitly requested by the user.

### Container deploy procedure (MANDATORY)

Azure App Service caches the `:latest` Docker tag digest. A plain `az webapp restart` does **NOT** re-pull the image. Every deploy **must** use a unique tag to force a pull:

```powershell
# Use deploy.ps1 (preferred):
.\deploy.ps1

# Or manually:
$tag = Get-Date -Format "yyyyMMddHHmmss"
az acr build --registry platysearchacr --image "platysearch:$tag" --image "platysearch:latest" --no-logs .
az webapp config container set --name platysearch-app --resource-group platysearch-rg --container-image-name "platysearchacr.azurecr.io/platysearch:$tag"
az webapp restart --name platysearch-app --resource-group platysearch-rg
```

**Never** rely on `az webapp restart` alone after an ACR build — it will keep running the old image.

### Azure resources

| Resource | Name |
|---|---|
| ACR | `platysearchacr` |
| App Service | `platysearch-app` |
| Resource Group | `platysearch-rg` |
| Domain | `platysearch.platysoft.com` |

### Admin authentication

All `/admin/*` and `/debug/*` routes require login via `PLATY_ADMIN_PASSWORD` (set as an Azure App Setting). The password is checked against a cookie-based session (HMAC-signed, 24 h TTL, httponly + secure). Login page: `/admin/login`. Set the password:

```powershell
az webapp config appsettings set --name platysearch-app --resource-group platysearch-rg --settings PLATY_ADMIN_PASSWORD="<password>"
```

## Coding Conventions

- Use `ruff` for linting (`pyproject.toml` config)
- Type hints everywhere; `from __future__ import annotations` at top of modules
- Async-first — database and HTTP calls are all async
- Settings via `pydantic-settings` with `PLATY_` env prefix
- Tests in `tests/` using `pytest` + `pytest-asyncio`

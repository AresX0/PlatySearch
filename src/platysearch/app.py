"""FastAPI application — search API and web UI."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pathlib import Path

from platysearch.ranker import search, SearchResult


@asynccontextmanager
async def lifespan(app: FastAPI):
    from platysearch.scheduler import start_scheduler, stop_scheduler

    start_scheduler()
    yield
    stop_scheduler()


app = FastAPI(title="PlatySearch", version="0.1.0", lifespan=lifespan)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


_TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

_STATIC_DIR = Path(__file__).parent / "static"
if _STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")


# ── API ──────────────────────────────────────────────────────────────────────


_VALID_TABS = {"all", "news", "images", "video"}


@app.get("/api/search")
async def api_search(
    q: str = Query(..., min_length=1, max_length=500),
    tab: str = Query("all"),
    limit: int = Query(20, ge=1, le=100),
) -> list[dict]:
    tab = tab if tab in _VALID_TABS else "all"
    results = await search(q, tab=tab, limit=limit)
    return [
        {
            "url": r.url,
            "title": r.title,
            "snippet": r.snippet,
            "score": round(r.score, 4),
            "ai_score": round(r.ai_score, 4),
        }
        for r in results
    ]


# ── Web UI ───────────────────────────────────────────────────────────────────


@app.get("/", response_class=HTMLResponse)
async def home(request: Request, q: str | None = None, tab: str = "all"):
    tab = tab if tab in _VALID_TABS else "all"
    results: list[SearchResult] = []
    if q:
        results = await search(q, tab=tab)
    return templates.TemplateResponse(
        "search.html",
        {"request": request, "query": q or "", "results": results, "tab": tab},
    )

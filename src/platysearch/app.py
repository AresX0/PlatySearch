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


@app.get("/debug/db")
async def debug_db() -> dict:
    """Temporary debug endpoint to inspect the deployed DB."""
    from platysearch.database import get_db
    from platysearch.config import get_settings
    import os

    settings = get_settings()
    db_path = str(settings.db_path)
    exists = os.path.exists(db_path)
    size_mb = round(os.path.getsize(db_path) / (1024 * 1024), 1) if exists else 0

    # Debug env
    env_val = os.environ.get("PLATY_DB_PATH", "<not set>")

    tables: list[str] = []
    row_counts: dict[str, int] = {}
    if exists:
        db = await get_db()
        try:
            rows = await db.execute_fetchall(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
            tables = [r[0] for r in rows]
            for t in tables:
                cnt = await db.execute_fetchall(f"SELECT COUNT(*) FROM [{t}]")
                row_counts[t] = cnt[0][0]
        finally:
            await db.close()
    return {
        "db_path": db_path,
        "env_PLATY_DB_PATH": env_val,
        "exists": exists,
        "size_mb": size_mb,
        "tables": tables,
        "row_counts": row_counts,
    }


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
    fallback = False
    if q:
        results = await search(q, tab=tab)
        if not results and tab != "all":
            results = await search(q, tab="all")
            fallback = True
    return templates.TemplateResponse(
        "search.html",
        {
            "request": request,
            "query": q or "",
            "results": results,
            "tab": tab,
            "fallback": fallback,
        },
    )

"""FastAPI application — search API and web UI."""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pathlib import Path

from platysearch.ranker import search, SearchResult


@asynccontextmanager
async def lifespan(app: FastAPI):
    from platysearch.database import init_db
    from platysearch.scheduler import start_scheduler, stop_scheduler

    await init_db()
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
            **({"image_url": r.image_url, "image_alt": r.image_alt} if r.image_url else {}),
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


# ── Admin / debug endpoints ─────────────────────────────────────────────────


@app.get("/debug/db")
async def debug_db() -> dict:
    """Return DB stats (page counts, tables, size)."""
    import aiosqlite
    from platysearch.config import get_settings

    settings = get_settings()
    db_path = Path(settings.db_path)
    info: dict = {
        "db_path": str(db_path),
        "env_PLATY_DB_PATH": os.environ.get("PLATY_DB_PATH", ""),
        "exists": db_path.exists(),
        "size_mb": round(db_path.stat().st_size / (1024 * 1024), 1) if db_path.exists() else 0,
    }
    if db_path.exists():
        async with aiosqlite.connect(str(db_path)) as db:
            rows = await db.execute_fetchall(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
            tables = [r[0] for r in rows]
            info["tables"] = tables
            counts = {}
            for t in tables:
                row = await db.execute_fetchall(f"SELECT COUNT(*) FROM [{t}]")  # noqa: S608
                counts[t] = row[0][0]
            info["row_counts"] = counts
    return info


@app.post("/admin/reindex")
async def admin_reindex() -> dict:
    """Trigger a full index + score rebuild in the background."""
    from platysearch.indexer import build_index
    from platysearch.ranker import compute_scores

    async def _run():
        await build_index()
        await compute_scores()

    asyncio.create_task(_run())
    return {"status": "started", "message": "Index + score rebuild started in background"}


@app.post("/admin/init-db")
async def admin_init_db() -> dict:
    """Ensure all tables exist (e.g. after schema changes)."""
    from platysearch.database import init_db

    await init_db()
    return {"status": "ok", "message": "Database schema initialised"}

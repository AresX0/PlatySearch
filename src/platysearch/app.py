"""FastAPI application — search API and web UI."""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response as FastAPIResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pathlib import Path

from platysearch.ranker import search, SearchResult


@asynccontextmanager
async def lifespan(app: FastAPI):
    from platysearch.database import init_db
    from platysearch.federation import init_federation_db
    from platysearch.scheduler import start_scheduler, stop_scheduler

    await init_db()
    await init_federation_db()
    start_scheduler()
    yield
    stop_scheduler()


app = FastAPI(title="PlatySearch", version="0.1.0", lifespan=lifespan)


from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response


class _CSPMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response: Response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "frame-ancestors 'self' https://platysoft.com https://*.platysoft.com"
        )
        # Remove X-Frame-Options so the CSP frame-ancestors directive takes
        # precedence (Azure App Service may inject SAMEORIGIN by default).
        if "X-Frame-Options" in response.headers:
            del response.headers["X-Frame-Options"]
        return response


app.add_middleware(_CSPMiddleware)


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
    federated_results: list = []
    fallback = False
    if q:
        results = await search(q, tab=tab)

        # Query federated peers concurrently.
        from platysearch.federation import query_all_peers, FederatedResult
        federated_results = await query_all_peers(q, tab=tab, limit=10)

        if not results and not federated_results and tab != "all":
            results = await search(q, tab="all")
            federated_results = await query_all_peers(q, tab="all", limit=10)
            fallback = True
    return templates.TemplateResponse(
        "search.html",
        {
            "request": request,
            "query": q or "",
            "results": results,
            "federated_results": federated_results,
            "tab": tab,
            "fallback": fallback,
        },
    )


# ── Admin auth ───────────────────────────────────────────────────────────────


@app.get("/admin/login", response_class=HTMLResponse)
async def admin_login_page(request: Request, next: str = "/admin/dashboard"):
    from platysearch.auth import is_authenticated

    if is_authenticated(request):
        return RedirectResponse(next, status_code=303)
    return templates.TemplateResponse(
        "login.html",
        {"request": request, "error": "", "next_url": next},
    )


@app.post("/admin/login")
async def admin_login(request: Request, password: str = Form(...), next: str = Form("/admin/dashboard")):
    from platysearch.auth import check_password, create_session_cookie

    if not check_password(password):
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "Invalid password.", "next_url": next},
            status_code=401,
        )
    cookie_val, cookie_name = create_session_cookie()
    resp = RedirectResponse(next, status_code=303)
    resp.set_cookie(
        cookie_name, cookie_val,
        httponly=True, secure=True, samesite="lax", max_age=86400,
    )
    return resp


@app.get("/admin/logout")
async def admin_logout():
    resp = RedirectResponse("/admin/login", status_code=303)
    resp.delete_cookie("ps_admin")
    return resp


# ── Admin Dashboard ──────────────────────────────────────────────────────────


@app.get("/admin/dashboard", response_class=HTMLResponse)
async def admin_dashboard(request: Request, message: str = "", message_type: str = ""):
    from platysearch.auth import require_admin

    if redirect := require_admin(request):
        return redirect

    import aiosqlite
    from platysearch.config import get_settings
    from platysearch.database import list_custom_seeds
    from platysearch.scheduler import (
        _NIGHTLY_SEEDS,
        get_current_job,
        get_job_history,
        get_next_run_times,
    )

    settings = get_settings()
    db_path = Path(settings.db_path)

    # Gather DB stats
    db_stats: dict = {"pages": 0, "postings": 0, "db_size_mb": 0, "queue": 0}
    if db_path.exists():
        db_stats["db_size_mb"] = db_path.stat().st_size / (1024 * 1024)
        try:
            async with aiosqlite.connect(str(db_path)) as db:
                row = await db.execute_fetchall("SELECT COUNT(*) FROM pages")
                db_stats["pages"] = row[0][0]
                row = await db.execute_fetchall("SELECT COUNT(*) FROM postings")
                db_stats["postings"] = row[0][0]
                row = await db.execute_fetchall("SELECT COUNT(*) FROM crawl_queue")
                db_stats["queue"] = row[0][0]
        except Exception:
            pass

    custom_seeds = await list_custom_seeds()
    history = await get_job_history()

    return templates.TemplateResponse(
        "admin.html",
        {
            "request": request,
            "current_job": get_current_job(),
            "history": history,
            "next_runs": get_next_run_times(),
            "db_stats": db_stats,
            "custom_seeds": custom_seeds,
            "builtin_seed_count": len(_NIGHTLY_SEEDS),
            "message": message,
            "message_type": message_type,
        },
    )


@app.get("/admin", response_class=HTMLResponse)
async def admin_redirect():
    return RedirectResponse("/admin/dashboard", status_code=303)


@app.post("/admin/trigger/crawl")
async def admin_trigger_crawl(request: Request):
    from platysearch.auth import require_admin

    if redirect := require_admin(request):
        return redirect

    from platysearch.scheduler import trigger_crawl

    try:
        await trigger_crawl()
        msg = "Crawl+index started"
        msg_type = "success"
    except RuntimeError as e:
        msg = str(e)
        msg_type = "error"
    return RedirectResponse(
        f"/admin/dashboard?message={msg}&message_type={msg_type}",
        status_code=303,
    )


@app.post("/admin/trigger/index")
async def admin_trigger_index(request: Request):
    from platysearch.auth import require_admin

    if redirect := require_admin(request):
        return redirect

    from platysearch.scheduler import trigger_index

    try:
        await trigger_index()
        msg = "Re-index started"
        msg_type = "success"
    except RuntimeError as e:
        msg = str(e)
        msg_type = "error"
    return RedirectResponse(
        f"/admin/dashboard?message={msg}&message_type={msg_type}",
        status_code=303,
    )


@app.post("/admin/trigger/stop")
async def admin_trigger_stop(request: Request):
    from platysearch.auth import require_admin

    if redirect := require_admin(request):
        return redirect

    from platysearch.scheduler import request_stop

    stopped = await request_stop()
    if stopped:
        msg = "Stop requested — job will finish current batch and halt"
        msg_type = "success"
    else:
        msg = "No job is currently running"
        msg_type = "error"
    return RedirectResponse(
        f"/admin/dashboard?message={msg}&message_type={msg_type}",
        status_code=303,
    )


@app.get("/admin/api/status")
async def admin_api_status(request: Request) -> dict:
    """JSON endpoint for live dashboard polling."""
    from platysearch.auth import is_authenticated

    if not is_authenticated(request):
        return {"error": "unauthorized"}

    import aiosqlite
    from platysearch.config import get_settings
    from platysearch.scheduler import get_current_job, get_job_history, get_next_run_times

    settings = get_settings()
    db_path = Path(settings.db_path)

    db_stats: dict = {"pages": 0, "postings": 0, "db_size_mb": 0, "queue": 0}
    if db_path.exists():
        db_stats["db_size_mb"] = round(db_path.stat().st_size / (1024 * 1024), 1)
        try:
            async with aiosqlite.connect(str(db_path)) as db:
                row = await db.execute_fetchall("SELECT COUNT(*) FROM pages")
                db_stats["pages"] = row[0][0]
                row = await db.execute_fetchall("SELECT COUNT(*) FROM postings")
                db_stats["postings"] = row[0][0]
                row = await db.execute_fetchall("SELECT COUNT(*) FROM crawl_queue")
                db_stats["queue"] = row[0][0]
        except Exception:
            pass

    job = get_current_job()
    current = None
    if job:
        current = {
            "job_type": job.job_type,
            "started_at": job.started_at.strftime("%Y-%m-%d %H:%M UTC"),
            "pages_crawled": job.pages_crawled,
            "pages_indexed": job.pages_indexed,
            "pages_scored": job.pages_scored,
        }

    history = await get_job_history()

    next_runs = {}
    for job_id, next_time in get_next_run_times().items():
        next_runs[job_id] = next_time.strftime("%Y-%m-%d %H:%M UTC") if next_time else None

    return {
        "current_job": current,
        "db_stats": db_stats,
        "history": history,
        "next_runs": next_runs,
    }


@app.post("/admin/seeds/add")
async def admin_seeds_add(request: Request, url: str = Form(...), category: str = Form("general")):
    from platysearch.auth import require_admin

    if redirect := require_admin(request):
        return redirect

    from platysearch.database import add_custom_seed

    ok = await add_custom_seed(url, category)
    if ok:
        from platysearch.scheduler import _persist_db
        from platysearch.config import get_settings
        _persist_db(get_settings().db_path)
        msg = "Seed added"
        msg_type = "success"
    else:
        msg = "Seed already exists"
        msg_type = "error"
    return RedirectResponse(
        f"/admin/dashboard?message={msg}&message_type={msg_type}",
        status_code=303,
    )


@app.post("/admin/seeds/remove")
async def admin_seeds_remove(request: Request, seed_id: int = Form(...)):
    from platysearch.auth import require_admin

    if redirect := require_admin(request):
        return redirect

    from platysearch.database import remove_custom_seed

    await remove_custom_seed(seed_id)
    from platysearch.scheduler import _persist_db
    from platysearch.config import get_settings
    _persist_db(get_settings().db_path)
    return RedirectResponse(
        "/admin/dashboard?message=Seed+removed&message_type=success",
        status_code=303,
    )


# ── Admin / debug endpoints ─────────────────────────────────────────────────


@app.get("/debug/db")
async def debug_db(request: Request) -> dict:
    """Return DB stats (page counts, tables, size)."""
    from platysearch.auth import require_admin

    if redirect := require_admin(request):
        return redirect

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
async def admin_reindex(request: Request) -> dict:
    from platysearch.auth import require_admin

    if redirect := require_admin(request):
        return redirect
    """Trigger a full index + score rebuild in the background."""
    from platysearch.ai_detector import score_all_pages
    from platysearch.indexer import compute_link_scores, index_all_pages

    async def _run():
        await index_all_pages()
        await compute_link_scores()
        await score_all_pages()

    asyncio.create_task(_run())
    return {"status": "started", "message": "Index + score rebuild started in background"}


@app.post("/admin/init-db")
async def admin_init_db(request: Request) -> dict:
    from platysearch.auth import require_admin

    if redirect := require_admin(request):
        return redirect
    """Ensure all tables exist (e.g. after schema changes)."""
    from platysearch.database import init_db

    await init_db()
    return {"status": "ok", "message": "Database schema initialised"}


# ── XRPC Federation endpoints ───────────────────────────────────────────────


@app.get("/xrpc/app.platysearch.server.describe")
async def xrpc_describe() -> dict:
    """Public — returns server metadata for federation discovery."""
    from platysearch.federation import describe_server

    return await describe_server()


@app.post("/xrpc/app.platysearch.server.register")
async def xrpc_register(request: Request) -> dict:
    """Public — handles incoming peer registration requests."""
    from platysearch.federation import is_enabled, handle_incoming_registration

    if not await is_enabled():
        return {"success": False, "message": "Federation is disabled on this server"}

    data = await request.json()
    return await handle_incoming_registration(data)


@app.get("/xrpc/app.platysearch.search.query")
async def xrpc_search_query(
    request: Request,
    q: str = Query(..., min_length=1, max_length=500),
    tab: str = Query("all"),
    limit: int = Query(20, ge=1, le=100),
) -> dict:
    """Authenticated — peers query this to get search results from our index."""
    from platysearch.federation import (
        get_peer_by_did, get_setting, is_enabled, verify_signature,
    )

    if not await is_enabled():
        return {"error": "Federation is disabled", "results": []}
    if (await get_setting("share_results")) != "1":
        return {"error": "Result sharing is disabled", "results": []}

    # Verify HMAC signature.
    server_did = request.headers.get("X-PS-Server-DID", "")
    timestamp = request.headers.get("X-PS-Timestamp", "")
    signature = request.headers.get("X-PS-Signature", "")

    if not server_did or not timestamp or not signature:
        return {"error": "Missing authentication headers", "results": []}

    peer = await get_peer_by_did(server_did)
    if not peer:
        return {"error": "Unknown server DID", "results": []}
    if peer.status != "active":
        return {"error": "Server is not in active federation status", "results": []}

    if not verify_signature(
        peer.shared_secret, timestamp, "GET",
        "/xrpc/app.platysearch.search.query", signature,
    ):
        return {"error": "Invalid signature", "results": []}

    # Run the local search.
    tab = tab if tab in _VALID_TABS else "all"
    results = await search(q, tab=tab, limit=limit)
    return {
        "results": [
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
    }


# ── Admin Federation UI ─────────────────────────────────────────────────────


@app.get("/admin/federation", response_class=HTMLResponse)
async def admin_federation(request: Request, message: str = "", message_type: str = ""):
    """Render the federation admin dashboard."""
    from platysearch.auth import require_admin

    if redirect := require_admin(request):
        return redirect

    from platysearch.federation import (
        describe_server, get_setting, list_peers,
    )

    peers = await list_peers()
    server_info = await describe_server()
    settings_data = {
        "enabled": (await get_setting("enabled")) == "1",
        "mode": await get_setting("mode"),
        "share_results": (await get_setting("share_results")) == "1",
        "accept_results": (await get_setting("accept_results")) == "1",
    }
    stats = {
        "active": sum(1 for p in peers if p.status == "active"),
        "pending": sum(1 for p in peers if p.status == "pending"),
        "blocked": sum(1 for p in peers if p.status == "blocked"),
    }

    return templates.TemplateResponse(
        "federation.html",
        {
            "request": request,
            "peers": peers,
            "server_info": server_info,
            "settings": settings_data,
            "stats": stats,
            "message": message,
            "message_type": message_type,
        },
    )


@app.post("/admin/federation/settings")
async def admin_federation_settings(
    request: Request,
    enabled: str = Form("0"),
    mode: str = Form("approve"),
    share_results: str = Form("0"),
    accept_results: str = Form("0"),
):
    """Save federation settings."""
    from platysearch.auth import require_admin

    if redirect := require_admin(request):
        return redirect

    from platysearch.federation import set_setting

    await set_setting("enabled", "1" if enabled == "1" else "0")
    await set_setting("mode", mode if mode in ("approve", "open") else "approve")
    await set_setting("share_results", "1" if share_results == "1" else "0")
    await set_setting("accept_results", "1" if accept_results == "1" else "0")

    return RedirectResponse(
        "/admin/federation?message=Settings+saved&message_type=success",
        status_code=303,
    )


@app.post("/admin/federation/register")
async def admin_federation_register(
    request: Request,
    peer_url: str = Form(...),
):
    """Register with a remote peer."""
    from platysearch.auth import require_admin

    if redirect := require_admin(request):
        return redirect

    from platysearch.federation import register_with_peer

    result = await register_with_peer(peer_url)
    msg_type = "success" if result["success"] else "error"
    msg = result["message"]
    return RedirectResponse(
        f"/admin/federation?message={msg}&message_type={msg_type}",
        status_code=303,
    )


@app.post("/admin/federation/approve")
async def admin_federation_approve(
    request: Request,
    peer_id: int = Form(...),
):
    """Approve a pending peer."""
    from platysearch.auth import require_admin

    if redirect := require_admin(request):
        return redirect

    from platysearch.federation import approve_peer

    await approve_peer(peer_id)
    return RedirectResponse(
        "/admin/federation?message=Peer+approved&message_type=success",
        status_code=303,
    )


@app.post("/admin/federation/block")
async def admin_federation_block(
    request: Request,
    peer_id: int = Form(...),
):
    """Block a peer."""
    from platysearch.auth import require_admin

    if redirect := require_admin(request):
        return redirect

    from platysearch.federation import block_peer

    await block_peer(peer_id)
    return RedirectResponse(
        "/admin/federation?message=Peer+blocked&message_type=success",
        status_code=303,
    )


@app.post("/admin/federation/remove")
async def admin_federation_remove(
    request: Request,
    peer_id: int = Form(...),
):
    """Remove a peer entirely."""
    from platysearch.auth import require_admin

    if redirect := require_admin(request):
        return redirect

    from platysearch.federation import remove_peer

    await remove_peer(peer_id)
    return RedirectResponse(
        "/admin/federation?message=Peer+removed&message_type=success",
        status_code=303,
    )

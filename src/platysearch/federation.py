"""Federation — distributed search network using XRPC-style protocol.

Allows PlatySearch instances to share search results with each other.
Follows the same federation pattern as PlatypusMatch:
  - HMAC-SHA256 signed requests between peers
  - Peer registration, approval, blocking
  - Admin-configurable settings stored in DB
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

import aiohttp

from platysearch.config import get_settings

log = logging.getLogger(__name__)

# ── Protocol constants ───────────────────────────────────────────────────────

PROTOCOL_VERSION = "1.0.0"
SIGNATURE_MAX_AGE = 300  # 5 minutes
QUERY_TIMEOUT = 8  # seconds per peer

LEXICONS = [
    "app.platysearch.server.describe",
    "app.platysearch.server.register",
    "app.platysearch.search.query",
]

# ── DB helpers ───────────────────────────────────────────────────────────────

_FEDERATION_SCHEMA = """
CREATE TABLE IF NOT EXISTS federation_peers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    did             TEXT    UNIQUE NOT NULL,
    url             TEXT    UNIQUE NOT NULL,
    name            TEXT    NOT NULL DEFAULT '',
    admin_email     TEXT    DEFAULT '',
    shared_secret   TEXT    NOT NULL,
    protocol_version TEXT   DEFAULT '1.0.0',
    status          TEXT    NOT NULL DEFAULT 'pending'
                    CHECK(status IN ('pending','active','blocked')),
    block_reason    TEXT    DEFAULT '',
    last_sync_at    TEXT,
    last_seen_at    TEXT,
    approved_at     TEXT,
    blocked_at      TEXT,
    created_at      TEXT    DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS federation_settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_federation_peers_status ON federation_peers(status);
"""

# Default settings — stored in federation_settings table.
_DEFAULTS: dict[str, str] = {
    "enabled": "0",
    "mode": "approve",          # approve | open
    "share_results": "1",       # serve local results to peers
    "accept_results": "1",      # include remote results in local search
}


async def init_federation_db() -> None:
    """Create federation tables if they don't exist."""
    from platysearch.database import get_db

    db = await get_db()
    try:
        await db.executescript(_FEDERATION_SCHEMA)
        # Seed defaults.
        for key, value in _DEFAULTS.items():
            await db.execute(
                "INSERT OR IGNORE INTO federation_settings (key, value) VALUES (?, ?)",
                (key, value),
            )
        await db.commit()
    finally:
        await db.close()


async def get_setting(key: str) -> str:
    """Read a federation setting from DB, falling back to defaults."""
    from platysearch.database import get_db

    db = await get_db()
    try:
        row = await db.execute_fetchall(
            "SELECT value FROM federation_settings WHERE key = ?", (key,)
        )
        if row:
            return row[0][0]
        return _DEFAULTS.get(key, "")
    finally:
        await db.close()


async def set_setting(key: str, value: str) -> None:
    """Write a federation setting to DB."""
    from platysearch.database import get_db

    db = await get_db()
    try:
        await db.execute(
            "INSERT OR REPLACE INTO federation_settings (key, value) VALUES (?, ?)",
            (key, value),
        )
        await db.commit()
    finally:
        await db.close()


async def is_enabled() -> bool:
    return (await get_setting("enabled")) == "1"


# ── Server identity ─────────────────────────────────────────────────────────


def get_server_did(base_url: str) -> str:
    """Derive a did:web identifier from the server URL."""
    host = urlparse(base_url).netloc
    return f"did:web:{host}"


async def describe_server() -> dict:
    """Return server metadata (public XRPC endpoint)."""
    settings = get_settings()
    base_url = f"https://{settings.host}" if settings.host != "0.0.0.0" else ""
    from platysearch.database import get_db

    db = await get_db()
    try:
        row = await db.execute_fetchall("SELECT COUNT(*) FROM pages")
        page_count = row[0][0] if row else 0
    finally:
        await db.close()

    return {
        "did": get_server_did(base_url),
        "name": "PlatySearch",
        "version": PROTOCOL_VERSION,
        "pageCount": page_count,
        "federationEnabled": await is_enabled(),
        "lexicons": LEXICONS,
    }


# ── HMAC Signatures ─────────────────────────────────────────────────────────


def generate_signature(shared_secret: str, method: str, path: str) -> dict[str, str]:
    """Create signed headers for an outgoing request to a peer."""
    timestamp = str(int(time.time()))
    payload = f"{timestamp}.{method}.{path}"
    sig = hmac.new(shared_secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return {
        "X-PS-Timestamp": timestamp,
        "X-PS-Signature": sig,
    }


def verify_signature(
    shared_secret: str, timestamp: str, method: str, path: str, signature: str
) -> bool:
    """Verify an HMAC-SHA256 signature from a peer."""
    try:
        ts = int(timestamp)
    except (ValueError, TypeError):
        return False
    if abs(time.time() - ts) > SIGNATURE_MAX_AGE:
        return False
    payload = f"{timestamp}.{method}.{path}"
    expected = hmac.new(shared_secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


# ── Peer management ──────────────────────────────────────────────────────────


@dataclass
class Peer:
    id: int
    did: str
    url: str
    name: str
    admin_email: str
    shared_secret: str
    protocol_version: str
    status: str
    block_reason: str
    last_seen_at: str | None
    last_sync_at: str | None
    approved_at: str | None
    blocked_at: str | None
    created_at: str | None


def _row_to_peer(row) -> Peer:
    return Peer(
        id=row[0], did=row[1], url=row[2], name=row[3],
        admin_email=row[4], shared_secret=row[5],
        protocol_version=row[6], status=row[7],
        block_reason=row[8], last_seen_at=row[9],
        last_sync_at=row[10], approved_at=row[11],
        blocked_at=row[12], created_at=row[13],
    )


_PEER_COLS = (
    "id, did, url, name, admin_email, shared_secret, "
    "protocol_version, status, block_reason, last_seen_at, "
    "last_sync_at, approved_at, blocked_at, created_at"
)


async def list_peers(status: str | None = None) -> list[Peer]:
    from platysearch.database import get_db

    db = await get_db()
    try:
        if status:
            rows = await db.execute_fetchall(
                f"SELECT {_PEER_COLS} FROM federation_peers WHERE status = ? ORDER BY created_at DESC",
                (status,),
            )
        else:
            rows = await db.execute_fetchall(
                f"SELECT {_PEER_COLS} FROM federation_peers ORDER BY created_at DESC"
            )
        return [_row_to_peer(r) for r in rows]
    finally:
        await db.close()


async def get_peer_by_did(did: str) -> Peer | None:
    from platysearch.database import get_db

    db = await get_db()
    try:
        rows = await db.execute_fetchall(
            f"SELECT {_PEER_COLS} FROM federation_peers WHERE did = ?", (did,)
        )
        return _row_to_peer(rows[0]) if rows else None
    finally:
        await db.close()


async def get_peer_by_id(peer_id: int) -> Peer | None:
    from platysearch.database import get_db

    db = await get_db()
    try:
        rows = await db.execute_fetchall(
            f"SELECT {_PEER_COLS} FROM federation_peers WHERE id = ?", (peer_id,)
        )
        return _row_to_peer(rows[0]) if rows else None
    finally:
        await db.close()


async def handle_incoming_registration(data: dict) -> dict:
    """Process a registration request from a remote peer."""
    from platysearch.database import get_db

    url = data.get("serverUrl", "").rstrip("/")
    did = data.get("did", "")
    name = data.get("name", "")

    if not url or not did:
        return {"success": False, "message": "Missing serverUrl or did"}

    # Check if blocked.
    existing = await get_peer_by_did(did)
    if existing and existing.status == "blocked":
        return {"success": False, "message": "Server is blocked"}

    mode = await get_setting("mode")
    shared_secret = secrets.token_hex(32)
    status = "active" if mode == "open" else "pending"

    # Keep existing status if already known.
    if existing:
        status = existing.status

    db = await get_db()
    try:
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        await db.execute(
            """INSERT INTO federation_peers (did, url, name, admin_email, shared_secret,
                   protocol_version, status, approved_at, last_seen_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(did) DO UPDATE SET
                   url=excluded.url, name=excluded.name,
                   admin_email=excluded.admin_email,
                   shared_secret=excluded.shared_secret,
                   protocol_version=excluded.protocol_version,
                   status=CASE WHEN federation_peers.status='blocked'
                              THEN federation_peers.status ELSE excluded.status END,
                   last_seen_at=excluded.last_seen_at""",
            (
                did, url, name,
                data.get("adminEmail", ""),
                data.get("sharedSecret", shared_secret),
                data.get("version", PROTOCOL_VERSION),
                status,
                now if status == "active" else None,
                now,
            ),
        )
        await db.commit()
    finally:
        await db.close()

    base_url = get_settings().host
    return {
        "success": True,
        "did": get_server_did(base_url),
        "name": "PlatySearch",
        "version": PROTOCOL_VERSION,
        "sharedSecret": shared_secret,
        "status": status,
        "message": (
            "Registration received - awaiting admin approval"
            if status == "pending"
            else "Registration accepted"
        ),
    }


async def register_with_peer(peer_url: str) -> dict:
    """Send a registration request to a remote PlatySearch instance."""
    endpoint = peer_url.rstrip("/") + "/xrpc/app.platysearch.server.register"
    shared_secret = secrets.token_hex(32)

    settings = get_settings()
    # Build our public URL from env or settings.
    import os
    public_url = os.environ.get("PLATY_PUBLIC_URL", f"https://{settings.host}")

    payload = {
        "serverUrl": public_url,
        "did": get_server_did(public_url),
        "name": "PlatySearch",
        "sharedSecret": shared_secret,
        "version": PROTOCOL_VERSION,
    }

    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=15)
        ) as session:
            async with session.post(endpoint, json=payload) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    # Store the peer.
                    from platysearch.database import get_db

                    db = await get_db()
                    try:
                        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                        await db.execute(
                            """INSERT INTO federation_peers
                                   (did, url, name, shared_secret, protocol_version, status,
                                    approved_at, last_seen_at)
                               VALUES (?, ?, ?, ?, ?, 'active', ?, ?)
                               ON CONFLICT(did) DO UPDATE SET
                                   url=excluded.url, name=excluded.name,
                                   shared_secret=excluded.shared_secret,
                                   status='active', approved_at=excluded.approved_at,
                                   last_seen_at=excluded.last_seen_at""",
                            (
                                data.get("did", get_server_did(peer_url)),
                                peer_url.rstrip("/"),
                                data.get("name", urlparse(peer_url).netloc),
                                data.get("sharedSecret", shared_secret),
                                data.get("version", PROTOCOL_VERSION),
                                now, now,
                            ),
                        )
                        await db.commit()
                    finally:
                        await db.close()

                    return {"success": True, "message": data.get("message", "Registered")}
                else:
                    return {"success": False, "message": f"Peer returned HTTP {resp.status}"}
    except Exception as exc:
        log.error("Federation: Failed to register with %s: %s", peer_url, exc)
        return {"success": False, "message": f"Connection failed: {exc}"}


async def approve_peer(peer_id: int) -> bool:
    from platysearch.database import get_db

    db = await get_db()
    try:
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        await db.execute(
            "UPDATE federation_peers SET status='active', approved_at=? WHERE id=?",
            (now, peer_id),
        )
        await db.commit()
        return True
    finally:
        await db.close()


async def block_peer(peer_id: int, reason: str = "") -> bool:
    from platysearch.database import get_db

    db = await get_db()
    try:
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        await db.execute(
            "UPDATE federation_peers SET status='blocked', block_reason=?, blocked_at=? WHERE id=?",
            (reason, now, peer_id),
        )
        await db.commit()
        return True
    finally:
        await db.close()


async def remove_peer(peer_id: int) -> bool:
    from platysearch.database import get_db

    db = await get_db()
    try:
        await db.execute("DELETE FROM federation_peers WHERE id=?", (peer_id,))
        await db.commit()
        return True
    finally:
        await db.close()


# ── Federated search (client side) ──────────────────────────────────────────


@dataclass
class FederatedResult:
    """A search result returned from a remote peer."""
    url: str
    title: str
    snippet: str
    score: float
    ai_score: float
    peer_name: str
    peer_url: str
    image_url: str = ""
    image_alt: str = ""


async def query_peer(
    peer: Peer, query: str, tab: str = "all", limit: int = 10
) -> list[FederatedResult]:
    """Send a search query to a single peer and return results."""
    endpoint = peer.url.rstrip("/") + "/xrpc/app.platysearch.search.query"
    path = "/xrpc/app.platysearch.search.query"
    headers = generate_signature(peer.shared_secret, "GET", path)
    headers["X-PS-Server-DID"] = get_server_did(
        get_settings().host
    )

    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=QUERY_TIMEOUT)
        ) as session:
            async with session.get(
                endpoint,
                params={"q": query, "tab": tab, "limit": str(limit)},
                headers=headers,
            ) as resp:
                if resp.status != 200:
                    log.warning("Federation: Peer %s returned %d", peer.url, resp.status)
                    return []
                data = await resp.json()
                results = []
                for item in data.get("results", []):
                    results.append(FederatedResult(
                        url=item.get("url", ""),
                        title=item.get("title", ""),
                        snippet=item.get("snippet", ""),
                        score=float(item.get("score", 0)),
                        ai_score=float(item.get("ai_score", 0)),
                        peer_name=peer.name or peer.url,
                        peer_url=peer.url,
                        image_url=item.get("image_url", ""),
                        image_alt=item.get("image_alt", ""),
                    ))
                # Update last_seen.
                from platysearch.database import get_db

                db = await get_db()
                try:
                    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                    await db.execute(
                        "UPDATE federation_peers SET last_seen_at=? WHERE id=?",
                        (now, peer.id),
                    )
                    await db.commit()
                finally:
                    await db.close()

                return results
    except Exception as exc:
        log.warning("Federation: Failed to query peer %s: %s", peer.url, exc)
        return []


async def query_all_peers(
    query: str, tab: str = "all", limit: int = 10
) -> list[FederatedResult]:
    """Query all active peers concurrently and merge results."""
    import asyncio

    if not await is_enabled():
        return []
    if (await get_setting("accept_results")) != "1":
        return []

    peers = await list_peers(status="active")
    if not peers:
        return []

    tasks = [query_peer(p, query, tab, limit) for p in peers]
    all_results_lists = await asyncio.gather(*tasks, return_exceptions=True)

    merged: list[FederatedResult] = []
    for result in all_results_lists:
        if isinstance(result, Exception):
            log.warning("Federation: Peer query error: %s", result)
            continue
        merged.extend(result)

    # Sort by score descending, cap total.
    merged.sort(key=lambda r: r.score, reverse=True)
    return merged[:limit * 2]  # return more than limit so caller can interleave

"""Admin authentication — cookie-based session with HMAC-signed tokens."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time

from fastapi import Request
from fastapi.responses import RedirectResponse

from platysearch.config import get_settings

# Token is valid for 24 hours.
_TOKEN_TTL = 86400

# Secret key generated once per process (tokens invalidate on restart, which is fine).
_SESSION_SECRET: str = secrets.token_hex(32)


def _sign_token(timestamp: str) -> str:
    return hmac.new(
        _SESSION_SECRET.encode(), timestamp.encode(), hashlib.sha256
    ).hexdigest()


def create_session_cookie() -> tuple[str, str]:
    """Return (cookie_value, cookie_name) for a valid admin session."""
    ts = str(int(time.time()))
    sig = _sign_token(ts)
    return f"{ts}.{sig}", "ps_admin"


def verify_session(cookie_value: str) -> bool:
    """Check that the cookie contains a valid, non-expired signed token."""
    if not cookie_value or "." not in cookie_value:
        return False
    ts, sig = cookie_value.split(".", 1)
    try:
        issued = int(ts)
    except ValueError:
        return False
    if time.time() - issued > _TOKEN_TTL:
        return False
    return hmac.compare_digest(sig, _sign_token(ts))


def is_authenticated(request: Request) -> bool:
    """Return True if the request carries a valid admin session cookie."""
    cookie = request.cookies.get("ps_admin", "")
    return verify_session(cookie)


def require_admin(request: Request) -> RedirectResponse | None:
    """If not authenticated, return a redirect to login. Else return None."""
    if is_authenticated(request):
        return None
    return RedirectResponse(
        f"/admin/login?next={request.url.path}", status_code=303
    )


def check_password(password: str) -> bool:
    """Verify the supplied password against the configured admin password."""
    expected = get_settings().admin_password
    if not expected:
        # No password configured — block all access.
        return False
    return hmac.compare_digest(password, expected)

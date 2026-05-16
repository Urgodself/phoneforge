"""PIN-based session auth for the PhoneForge web UI.

Single-user, single-PIN — no registration, no password reset, no MFA. Auth
state lives entirely inside a signed Starlette session cookie. The PIN and
the cookie-signing secret are both read from env at app construction time;
they never appear in logs, never get sent to the browser.

Why session-cookie auth (and not JWT / HTTP basic):
- HTTP basic re-sends the PIN on every request — worse leak surface.
- JWTs would require a separate refresh story for "30-day rolling" sessions.
- Starlette's `SessionMiddleware` is itsdangerous-backed and battle-tested.

A fixed 30-day TTL is intentional: rolling expiration on a signed cookie
is non-trivial to implement correctly, and for a single-user tool the
re-auth UX cost (one PIN entry per month) is negligible.
"""
from __future__ import annotations

import hmac
import logging
import os
import secrets
from typing import Awaitable, Callable

from fastapi import Request
from fastapi.responses import RedirectResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware

log = logging.getLogger(__name__)

# 30 days. Starlette's SessionMiddleware uses absolute max_age, not rolling.
SESSION_MAX_AGE_S = 60 * 60 * 24 * 30

# Routes that bypass the auth check. /static is permitted for completeness
# even though we currently don't ship any static files (Tailwind via CDN).
PUBLIC_PATHS: set[str] = {"/login", "/logout", "/health"}
PUBLIC_PREFIXES: tuple[str, ...] = ("/static/",)


def get_pin() -> str:
    """Read the PIN from PHONEFORGE_PIN env, falling back to '1991'.

    The fallback is intentional — the spec calls it out as a personal
    convenience PIN. In any deployment exposed to the public internet,
    PHONEFORGE_PIN should be overridden in `.env` (mode 600).
    """
    return (os.environ.get("PHONEFORGE_PIN") or "1991").strip()


def get_session_secret() -> str:
    """Read the cookie-signing secret. Generates ephemeral one if missing.

    A missing secret is logged loudly: an ephemeral secret means every
    web-server restart invalidates all existing sessions. That's
    acceptable for dev but should never happen in production.
    """
    secret = (os.environ.get("SESSION_SECRET") or "").strip()
    if not secret:
        log.warning(
            "SESSION_SECRET is unset — generating an ephemeral one. "
            "All sessions will invalidate on restart. Set SESSION_SECRET "
            "in .env to a 32-byte hex string for production."
        )
        return secrets.token_hex(32)
    if len(secret) < 32:
        log.warning("SESSION_SECRET is shorter than 32 chars — consider rotating to a 64-hex token.")
    return secret


def verify_pin(submitted: str) -> bool:
    """Constant-time PIN comparison.

    Single-user tool — timing attacks against a 4-digit PIN aren't a
    realistic threat, but hmac.compare_digest is free and removes the
    question from a code review.
    """
    return hmac.compare_digest((submitted or "").strip(), get_pin())


def is_authed(request: Request) -> bool:
    """True iff the request carries a valid signed session with authed=True."""
    try:
        return bool(request.session.get("authed"))
    except AssertionError:
        # SessionMiddleware not installed — defensive guard for unit tests.
        return False


class AuthMiddleware(BaseHTTPMiddleware):
    """Redirect unauthenticated requests to /login.

    Implemented as a `BaseHTTPMiddleware` subclass (not via the decorator)
    so it can be added AFTER `SessionMiddleware` with `add_middleware` —
    Starlette executes middlewares in reverse add order, so we want the
    session installed first, then this gate on top of it.

    Whitelist:
      - GET/POST /login   — the login form itself
      - POST     /logout  — clearing a session must not require one
      - GET      /health  — uptime probe, no auth
      - /static/*         — public assets (reserved; none right now)

    Anything else without `authed=True` in the session → 302 /login.
    """

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        path = request.url.path
        if path in PUBLIC_PATHS or any(path.startswith(p) for p in PUBLIC_PREFIXES):
            return await call_next(request)
        if is_authed(request):
            return await call_next(request)
        return RedirectResponse(url="/login", status_code=302)

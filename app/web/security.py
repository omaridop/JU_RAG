"""Lightweight, dependency-free CSRF protection + security headers.

A per-session token is embedded in every server-rendered form (``csrf_token()``
Jinja global) and validated on every unsafe request to the admin/auth
blueprints. The public JSON chat API is same-origin ``fetch`` and is left to
SameSite cookies + rate limiting, so adding a token there is unnecessary.
"""

from __future__ import annotations

import secrets

from flask import abort, request, session
from markupsafe import Markup

_FIELD = "csrf_token"
_HEADER = "X-CSRF-Token"
_SESSION_KEY = "_csrf"


def generate_csrf() -> str:
    """Return the session CSRF token, creating it on first use."""
    token = session.get(_SESSION_KEY)
    if not token:
        token = secrets.token_hex(32)
        session[_SESSION_KEY] = token
    return token


def validate_csrf(token: str) -> bool:
    expected = session.get(_SESSION_KEY)
    return bool(expected) and bool(token) and secrets.compare_digest(token, expected)


def csrf_field() -> Markup:
    """Hidden input carrying the CSRF token, for server-rendered forms."""
    return Markup(f'<input type="hidden" name="{_FIELD}" value="{generate_csrf()}">')


def csrf_protect() -> None:
    """before_request guard: reject unsafe requests without a valid token."""
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        token = request.form.get(_FIELD) or request.headers.get(_HEADER, "")
        if not validate_csrf(token):
            abort(400, description="Invalid or missing CSRF token. Please reload and retry.")


SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "SAMEORIGIN",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    # Allows the app's own inline scripts/styles + Google Fonts, blocks framing
    # and restricts form submissions to same-origin (an extra CSRF defense).
    "Content-Security-Policy": (
        "default-src 'self'; "
        "media-src 'self' blob:; "
        "img-src 'self' data:; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "script-src 'self' 'unsafe-inline'; "
        "connect-src 'self'; "
        "frame-ancestors 'self'; base-uri 'self'; form-action 'self'"
    ),
}

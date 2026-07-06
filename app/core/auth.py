"""Admin authentication: password hashing, login/logout, session guard.

A single admin account is seeded from ``ADMIN_USERNAME`` / ``ADMIN_PASSWORD``
on first run. Additional admins can be created via :func:`create_admin`.
"""

from __future__ import annotations

import functools
import logging
import time
from typing import Optional

from flask import flash, redirect, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from ..config import settings
from . import db

log = logging.getLogger(__name__)

SESSION_KEY = "admin_id"


def hash_password(password: str) -> str:
    return generate_password_hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    return check_password_hash(password_hash, password)


def seed_admin() -> None:
    """Create the initial SUPER admin from config if no admin exists yet."""
    if db.count_admins() == 0:
        db.create_admin(settings.ADMIN_USERNAME, hash_password(settings.ADMIN_PASSWORD),
                        role="super", created_by="seed")
        log.info("Seeded initial super-admin account '%s'.", settings.ADMIN_USERNAME)


def _client_ip() -> str:
    try:
        return request.headers.get("X-Forwarded-For", request.remote_addr or "") or ""
    except RuntimeError:  # outside a request context (CLI)
        return ""


def login(username: str, password: str) -> bool:
    admin = db.get_admin_by_username(username)
    if admin and verify_password(admin["password_hash"], password):
        session.clear()
        session[SESSION_KEY] = admin["id"]
        session["admin_seen"] = time.time()
        # Non-permanent => the cookie is dropped when the browser closes, so the
        # admin must re-enter the password on the next visit.
        session.permanent = False
        db.add_audit(admin_id=admin["id"], username=admin["username"],
                     action="login", ip=_client_ip())
        return True
    # Record the failed attempt (security trail for the super admin).
    db.add_audit(admin_id=None, username=username or "?", action="login_failed",
                 ip=_client_ip())
    return False


def logout() -> None:
    session.pop(SESSION_KEY, None)
    session.pop("admin_seen", None)


def current_admin() -> Optional[dict]:
    admin_id = session.get(SESSION_KEY)
    if admin_id is None:
        return None
    # Server-side idle timeout (robust even if the browser restores the cookie).
    timeout = settings.ADMIN_SESSION_MINUTES * 60
    if timeout > 0:
        last_seen = float(session.get("admin_seen", 0) or 0)
        if time.time() - last_seen > timeout:
            session.pop(SESSION_KEY, None)
            session.pop("admin_seen", None)
            return None
        session["admin_seen"] = time.time()  # refresh on activity
    return db.get_admin(int(admin_id))


def is_authenticated() -> bool:
    return current_admin() is not None


def is_super(admin: Optional[dict] = None) -> bool:
    admin = admin if admin is not None else current_admin()
    return bool(admin) and admin.get("role") == "super"


def login_required(view):
    """Redirect to the login page if the request is not authenticated."""

    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if not is_authenticated():
            flash("يرجى تسجيل الدخول للوصول إلى لوحة التحكم.", "warning")
            return redirect(url_for("auth.login", next=request.path))
        return view(*args, **kwargs)

    return wrapped


def super_required(view):
    """Allow only the super admin (admin management, audit trail)."""

    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        admin = current_admin()
        if admin is None:
            flash("يرجى تسجيل الدخول للوصول إلى لوحة التحكم.", "warning")
            return redirect(url_for("auth.login", next=request.path))
        if admin.get("role") != "super":
            flash("هذه الصفحة متاحة للمشرف الرئيسي فقط.", "error")
            return redirect(url_for("admin.dashboard"))
        return view(*args, **kwargs)

    return wrapped

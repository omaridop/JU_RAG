"""Flask application factory."""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta

from flask import Flask, jsonify, url_for

from ..config import settings
from ..core import auth, db
from ..core.utils import parse_dt
from .security import SECURITY_HEADERS, csrf_field, generate_csrf


def create_app() -> Flask:
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config.update(
        SECRET_KEY=settings.SECRET_KEY,
        MAX_CONTENT_LENGTH=settings.max_content_length,
        PERMANENT_SESSION_LIFETIME=timedelta(days=7),
        JSON_AS_ASCII=False,
        TEMPLATES_AUTO_RELOAD=True,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=settings.COOKIE_SECURE,
    )

    # Honour X-Forwarded-* only behind a trusted reverse proxy (real client IPs).
    if settings.TRUST_PROXY:
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    # One-time runtime setup.
    settings.ensure_dirs()
    db.init_db()
    auth.seed_admin()

    # CSRF helpers available to every template; security headers on every response.
    app.jinja_env.globals["csrf_token"] = generate_csrf
    app.jinja_env.globals["csrf_field"] = csrf_field

    # Cache-busting: append the file's mtime so browsers always fetch the latest
    # CSS/JS after an edit (no more stale-asset confusion).
    def _asset(filename: str) -> str:
        try:
            ver = int(os.path.getmtime(os.path.join(app.static_folder, filename)))
        except OSError:
            ver = 0
        return url_for("static", filename=filename, v=ver)

    app.jinja_env.globals["asset"] = _asset

    @app.after_request
    def _security_headers(resp):
        for k, v in SECURITY_HEADERS.items():
            resp.headers.setdefault(k, v)
        return resp

    @app.get("/healthz")
    def _healthz():
        from ..pipeline.vectorstore import get_store
        try:
            vectors = get_store().count()
            ok = True
        except Exception:  # noqa: BLE001
            vectors, ok = 0, False
        return jsonify({
            "status": "ok" if (ok and settings.is_configured()) else "degraded",
            "configured": settings.is_configured(),
            "vectors": vectors,
            "documents": db.get_dashboard_stats().get("documents", 0),
        }), (200 if ok else 503)

    # --- Jinja helpers ---------------------------------------------------- #
    @app.template_filter("fmt_date")
    def _fmt_date(value: str, with_time: bool = False) -> str:
        dt = parse_dt(value)
        if not dt:
            return value or "—"
        return dt.strftime("%Y-%m-%d %H:%M" if with_time else "%Y-%m-%d")

    _STATUS_AR = {
        "pending": "بالانتظار", "processing": "قيد المعالجة", "reading": "قيد القراءة",
        "embedding": "قيد التضمين", "storing": "قيد التخزين", "done": "مكتمل",
        "error": "خطأ", "skipped": "تم تخطيه",
    }
    _SERVED_AR = {
        "rag": "بحث وتوليد", "memory": "ذاكرة", "limit": "حد الجلسة", "error": "خطأ",
        "budget": "سقف الميزانية", "rate_limited": "تقييد المعدّل", "web": "بحث في الموقع",
    }

    @app.template_filter("status_ar")
    def _status_ar(value: str) -> str:
        return _STATUS_AR.get(value, value or "—")

    @app.template_filter("served_ar")
    def _served_ar(value: str) -> str:
        return _SERVED_AR.get(value, value or "—")

    @app.context_processor
    def _inject():
        return {
            "APP_NAME": "JU Assistant",
            "current_admin": auth.current_admin(),
            "current_year": datetime.now().year,
            "chat_model": settings.CHAT_MODEL,
            "chunk_model": settings.CHUNK_MODEL,
            "embed_model": settings.EMBED_MODEL,
        }

    # --- Blueprints ------------------------------------------------------- #
    # CSRF guards are attached in the blueprint modules themselves (one-time at
    # import), so create_app() stays re-callable (tests, multi-instance).
    from .routes.chat import bp as chat_bp
    from .routes.auth import bp as auth_bp
    from .routes.admin import bp as admin_bp

    app.register_blueprint(chat_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(admin_bp)

    # --- Error handlers --------------------------------------------------- #
    @app.errorhandler(413)
    def _too_large(_e):
        return ("File too large. Maximum upload size is "
                f"{settings.MAX_UPLOAD_MB} MB.", 413)

    logging.getLogger(__name__).info(
        "JU Assistant app created (chat=%s, reasoning=%s).",
        settings.CHAT_MODEL, settings.CHAT_REASONING_EFFORT)
    return app

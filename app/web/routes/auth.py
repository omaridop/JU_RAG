"""Admin authentication blueprint (login / logout)."""

from __future__ import annotations

from flask import (Blueprint, flash, redirect, render_template, request,
                   url_for)

from ...core import auth as auth_service
from ..security import csrf_protect

bp = Blueprint("auth", __name__, url_prefix="/admin")
bp.before_request(csrf_protect)  # CSRF-guard all unsafe requests to this blueprint


@bp.route("/login", methods=["GET", "POST"])
def login():
    if auth_service.is_authenticated():
        return redirect(url_for("admin.dashboard"))
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        if auth_service.login(username, password):
            nxt = request.args.get("next") or url_for("admin.dashboard")
            return redirect(nxt)
        flash("اسم المستخدم أو كلمة المرور غير صحيحة.", "error")
    return render_template("login.html")


@bp.get("/logout")
def logout():
    auth_service.logout()
    flash("تم تسجيل الخروج بنجاح.", "success")
    return redirect(url_for("auth.login"))

"""Admin panel blueprint: documents, logs, memory, settings."""

from __future__ import annotations

import json
import logging
import math
import re
import threading
import time
from pathlib import Path
from uuid import uuid4

from flask import (Blueprint, flash, jsonify, redirect, render_template,
                   request, url_for)
from werkzeug.utils import secure_filename

from ...config import settings
from ...core import db, tokens
from ...core.auth import (current_admin, hash_password, is_super, login_required,
                          super_required, verify_password)
from ...core.utils import file_sha256, now_iso, to_iso
from ...chatbot import memory_cache, speech
from ...pipeline.ingest import ingest_file
from ...pipeline.vectorstore import get_store
from ..security import csrf_protect

log = logging.getLogger(__name__)

bp = Blueprint("admin", __name__, url_prefix="/admin")
bp.before_request(csrf_protect)  # CSRF-guard all unsafe requests to this blueprint

_ALLOWED = {"pdf", "docx", "txt", "json"}
_ingest_lock = threading.Lock()  # serialize ingests (safer for the vector store)

_USERNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,31}$")
MIN_PASSWORD_LEN = 8


def _audit(action: str, target: str = "", detail: str = "") -> None:
    """Record an admin action; never let auditing break the action itself."""
    try:
        admin = current_admin() or {}
        ip = request.headers.get("X-Forwarded-For", request.remote_addr or "") or ""
        db.add_audit(admin_id=admin.get("id"), username=admin.get("username", "?"),
                     action=action, target=target[:300], detail=detail[:500], ip=ip)
    except Exception:  # noqa: BLE001
        log.exception("Audit write failed for action %s", action)


# Sentinel department that matches no rows — a non-super admin with no department
# assigned sees nothing until one is set.
_NO_DEPT = "\x00"


def _scope_department() -> "str | None":
    """Department filter for the current admin: None = super (sees every document)."""
    if is_super():
        return None
    return (current_admin() or {}).get("department") or _NO_DEPT


def _own_department() -> str:
    """The current admin's own department (empty for the super admin)."""
    return (current_admin() or {}).get("department") or ""


def _can_manage(doc) -> bool:
    """Whether the current admin may act on a document (own department, or super)."""
    if not doc:
        return False
    if is_super():
        return True
    dept = (current_admin() or {}).get("department")
    return bool(dept) and doc.get("department") == dept


# --------------------------------------------------------------------------- #
#  Dashboard                                                                  #
# --------------------------------------------------------------------------- #
@bp.get("/")
@login_required
def dashboard():
    stats = db.get_dashboard_stats()
    stats["vectors"] = get_store().count()
    return render_template("admin/dashboard.html", stats=stats)


# --------------------------------------------------------------------------- #
#  Documents (incremental ingest)                                             #
# --------------------------------------------------------------------------- #
def _background_ingest(path: str, doc_date: str, source_label: str,
                       uploaded_by: str, force: bool = False,
                       file_type: str = "", display_name: str = "") -> None:
    with _ingest_lock:
        try:
            ingest_file(path, doc_date=doc_date, source_label=source_label,
                        uploaded_by=uploaded_by, force=force,
                        file_type=file_type or None,
                        display_name=display_name or None)
        except Exception:  # noqa: BLE001
            log.exception("Background ingest failed for %s", path)


@bp.get("/documents")
@login_required
def documents():
    docs = db.list_documents(_scope_department())
    return render_template("admin/documents.html", documents=docs)


@bp.get("/documents/status")
@login_required
def documents_status():
    """JSON snapshot for live polling while files are processing."""
    docs = db.list_documents(_scope_department())
    return jsonify([
        {"id": d["id"], "status": d["status"], "chunk_count": d["chunk_count"],
         "error": d.get("error"), "progress": d.get("progress")}
        for d in docs
    ])


@bp.post("/documents/upload")
@login_required
def upload():
    file = request.files.get("file")
    doc_date = (request.form.get("doc_date") or "").strip()
    source_label = (request.form.get("source_label") or "").strip()

    if not file or not file.filename:
        flash("يرجى اختيار ملف للرفع.", "error")
        return redirect(url_for("admin.documents"))

    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    if ext not in _ALLOWED:
        flash("يُسمح بملفات PDF أو Word أو نص (TXT) أو أسئلة وأجوبة JSON فقط.", "error")
        return redirect(url_for("admin.documents"))

    # Admin MUST select the effective date for the document (recency).
    if not doc_date:
        flash("يرجى اختيار تاريخ سريان المستند.", "error")
        return redirect(url_for("admin.documents"))

    # Build a safe stored name that ALWAYS keeps the validated extension.
    # secure_filename strips non-ASCII, so an Arabic name like «الخطة.pdf»
    # collapses to just "pdf" — we keep the original name for display and
    # fall back to a uuid stem on disk.
    orig_name = file.filename
    stem = secure_filename(Path(orig_name).stem)
    # Empty, punctuation-only, or extension-only stems get a uuid instead.
    if not stem.strip("._-") or stem.lower() in _ALLOWED:
        stem = uuid4().hex[:12]
    stored = settings.UPLOAD_DIR / f"{int(time.time())}_{stem}.{ext}"
    file.save(str(stored))

    ok, msg, category = _kickoff_ingest(stored, ext, orig_name, doc_date, source_label,
                                        action="upload_document", department=_own_department())
    flash(msg, category)
    return redirect(url_for("admin.documents"))


def _kickoff_ingest(stored: Path, ext: str, orig_name: str, doc_date: str,
                    source_label: str, *, action: str, department: str = "") -> tuple[bool, str, str]:
    """Shared ingest launcher for both file uploads and typed text.

    Dedupes by content hash, creates the document row up-front (so the panel
    shows «pending» and polls), starts the background worker and audits.
    Returns ``(ok, flash_message, flash_category)``.
    """
    file_type = "qa" if ext == "json" else ext
    file_hash = file_sha256(stored)
    existing = db.get_document_by_hash(file_hash)
    if existing and existing["status"] == "done":
        stored.unlink(missing_ok=True)          # duplicate content; keep original
        return (False,
                f"«{orig_name}» موجود مسبقاً في قاعدة المعرفة "
                f"(«{existing['filename']}»). لم تتم إعادة المعالجة.", "error")

    admin = current_admin() or {}
    label = source_label or Path(orig_name).stem
    if not existing:
        db.add_document(
            filename=orig_name, stored_path=str(stored), file_hash=file_hash,
            file_type=file_type, source_label=label,
            doc_date=to_iso(doc_date) or now_iso(),
            uploaded_by=admin.get("username", "admin"), status="pending",
            department=department,
        )
    threading.Thread(
        target=_background_ingest,
        args=(str(stored), doc_date, label, admin.get("username", "admin"),
              bool(existing), file_type, orig_name),
        daemon=True,
    ).start()
    _audit(action, target=orig_name,
           detail=f"النوع: {file_type} | تاريخ السريان: {doc_date} | المصدر: {label}")
    return (True,
            f"تمت إضافة «{orig_name}» — تجري معالجته في الخلفية. "
            "تتم الإضافة إلى المخزن دون إعادة بنائه.", "success")


@bp.post("/documents/paste")
@login_required
def paste_text():
    """Ingest raw text typed/pasted directly in the panel as a RAG source."""
    title = (request.form.get("title") or "").strip()
    body = (request.form.get("text") or "").strip()
    doc_date = (request.form.get("doc_date") or "").strip()
    source_label = (request.form.get("source_label") or "").strip()

    if not title:
        flash("يرجى إدخال عنوان للنص.", "error")
        return redirect(url_for("admin.documents"))
    if len(body) < 20:
        flash("النص قصير جداً — يرجى إدخال محتوى كافٍ (20 حرفاً على الأقل).", "error")
        return redirect(url_for("admin.documents"))
    if not doc_date:
        flash("يرجى اختيار تاريخ سريان المحتوى.", "error")
        return redirect(url_for("admin.documents"))

    stem = secure_filename(title) or uuid4().hex[:12]
    stored = settings.UPLOAD_DIR / f"{int(time.time())}_{stem}.txt"
    stored.write_text(body, encoding="utf-8")

    orig_name = title if title.lower().endswith(".txt") else f"{title}.txt"
    ok, msg, category = _kickoff_ingest(stored, "txt", orig_name, doc_date,
                                        source_label or title, action="paste_text",
                                        department=_own_department())
    flash(msg, category)
    return redirect(url_for("admin.documents"))


@bp.post("/documents/<int:doc_id>/toggle")
@login_required
def toggle_document(doc_id: int):
    """Activate / deactivate a document. Deactivated = excluded from the bot."""
    doc = db.get_document(doc_id)
    if not doc:
        flash("المستند غير موجود.", "error")
        return redirect(url_for("admin.documents"))
    if not _can_manage(doc):
        flash("لا تملك صلاحية إدارة مستندات هذا القسم.", "error")
        return redirect(url_for("admin.documents"))
    new_active = not bool(doc.get("active", 1))
    db.set_document_active(doc_id, new_active)
    _audit("toggle_document", target=doc["filename"],
           detail=("تفعيل" if new_active else "تعطيل"))
    flash("تم تفعيل المستند — سيُستخدم في إجابات المساعد." if new_active
          else "تم تعطيل المستند — لن يُستخدم في الإجابات (كأنه محذوف).", "success")
    return redirect(url_for("admin.documents"))


@bp.post("/documents/<int:doc_id>/reingest")
@login_required
def reingest(doc_id: int):
    doc = db.get_document(doc_id)
    if not doc:
        flash("المستند غير موجود.", "error")
        return redirect(url_for("admin.documents"))
    if not _can_manage(doc):
        flash("لا تملك صلاحية إدارة مستندات هذا القسم.", "error")
        return redirect(url_for("admin.documents"))
    if not Path(doc["stored_path"]).exists():
        flash("الملف الأصلي غير موجود على القرص؛ تعذّرت إعادة المعالجة.", "error")
        return redirect(url_for("admin.documents"))
    admin = current_admin() or {}
    threading.Thread(
        target=_background_ingest,
        args=(doc["stored_path"], doc["doc_date"], doc.get("source_label") or "",
              admin.get("username", "admin"), True,
              doc.get("file_type") or "", doc.get("filename") or ""),
        daemon=True,
    ).start()
    _audit("reingest_document", target=doc["filename"], detail=f"#{doc_id}")
    flash("بدأت إعادة المعالجة في الخلفية.", "success")
    return redirect(url_for("admin.documents"))


@bp.post("/documents/<int:doc_id>/delete")
@login_required
def delete_document(doc_id: int):
    doc = db.get_document(doc_id)
    if not doc:
        flash("المستند غير موجود.", "error")
        return redirect(url_for("admin.documents"))
    if not _can_manage(doc):
        flash("لا تملك صلاحية إدارة مستندات هذا القسم.", "error")
        return redirect(url_for("admin.documents"))
    try:
        get_store().delete_doc(doc_id)
    except Exception:  # noqa: BLE001
        log.exception("Vector deletion failed for doc %s", doc_id)
    # Best-effort cleanup of files.
    for p in settings.PROCESSED_DIR.glob(f"{doc_id}_*.json"):
        p.unlink(missing_ok=True)
    Path(doc["stored_path"]).unlink(missing_ok=True)
    db.delete_document(doc_id)
    _audit("delete_document", target=doc["filename"],
           detail=f"#{doc_id} | {doc.get('chunk_count', 0)} سجل")
    flash(f"تم حذف «{doc['filename']}» ومتجهاته.", "success")
    return redirect(url_for("admin.documents"))


# --------------------------------------------------------------------------- #
#  Chat logs + promote to memory                                              #
# --------------------------------------------------------------------------- #
@bp.get("/logs")
@login_required
def logs():
    page = max(1, int(request.args.get("page", 1)))
    per = 20
    served = request.args.get("filter", "")
    query = request.args.get("q", "")
    rows, total = db.list_chat_logs(per, (page - 1) * per, served, query)
    for r in rows:
        try:
            r["sources"] = json.loads(r.get("sources_json") or "[]")
        except (json.JSONDecodeError, TypeError):
            r["sources"] = []
    pages = max(1, math.ceil(total / per))
    return render_template("admin/logs.html", logs=rows, page=page, pages=pages,
                           total=total, served=served, query=query)


@bp.post("/logs/<int:log_id>/promote")
@login_required
def promote(log_id: int):
    admin = current_admin() or {}
    try:
        chat = db.get_chat_log(log_id) or {}
        memory_cache.promote_log(log_id, created_by=admin.get("username", "admin"))
        _audit("promote_to_memory", target=(chat.get("question") or "")[:120],
               detail=f"سجل محادثة #{log_id}")
        flash("تم الحفظ في الذاكرة — ستُعاد هذه الإجابة دون استهلاك رموز.", "success")
    except Exception as exc:  # noqa: BLE001
        log.exception("Promote failed")
        flash(f"تعذّر الحفظ في الذاكرة: {exc}", "error")
    return redirect(request.referrer or url_for("admin.logs"))


# --------------------------------------------------------------------------- #
#  Memory CRUD                                                                #
# --------------------------------------------------------------------------- #
@bp.get("/memory")
@login_required
def memory():
    items = db.list_memory()
    return render_template("admin/memory.html", items=items)


@bp.post("/memory/add")
@login_required
def memory_add():
    q = (request.form.get("question") or "").strip()
    a = (request.form.get("answer") or "").strip()
    if not q or not a:
        flash("السؤال والإجابة مطلوبان.", "error")
        return redirect(url_for("admin.memory"))
    admin = current_admin() or {}
    try:
        memory_cache.add_manual(q, a, created_by=admin.get("username", "admin"))
        _audit("memory_add", target=q[:120])
        flash("تمت إضافة المدخل إلى الذاكرة.", "success")
    except Exception as exc:  # noqa: BLE001
        log.exception("Memory add failed")
        flash(f"تعذّرت الإضافة إلى الذاكرة: {exc}", "error")
    return redirect(url_for("admin.memory"))


@bp.post("/memory/<int:mem_id>/edit")
@login_required
def memory_edit(mem_id: int):
    q = (request.form.get("question") or "").strip()
    a = (request.form.get("answer") or "").strip()
    if not q or not a:
        flash("السؤال والإجابة مطلوبان.", "error")
        return redirect(url_for("admin.memory"))
    try:
        memory_cache.update(mem_id, question=q, answer=a)
        _audit("memory_edit", target=q[:120], detail=f"مدخل #{mem_id}")
        flash("تم تحديث المدخل.", "success")
    except Exception as exc:  # noqa: BLE001
        log.exception("Memory edit failed")
        flash(f"تعذّر تحديث المدخل: {exc}", "error")
    return redirect(url_for("admin.memory"))


@bp.post("/memory/<int:mem_id>/delete")
@login_required
def memory_delete(mem_id: int):
    try:
        row = db.get_memory(mem_id) or {}
        memory_cache.delete(mem_id)
        _audit("memory_delete", target=(row.get("question") or "")[:120],
               detail=f"مدخل #{mem_id}")
        flash("تم حذف المدخل.", "success")
    except Exception as exc:  # noqa: BLE001
        log.exception("Memory delete failed")
        flash(f"تعذّر حذف المدخل: {exc}", "error")
    return redirect(url_for("admin.memory"))


# --------------------------------------------------------------------------- #
#  Admin management (super admin only)                                        #
# --------------------------------------------------------------------------- #
@bp.get("/admins")
@super_required
def admins():
    return render_template("admin/admins.html", admins=db.list_admins(),
                           departments=db.list_departments())


@bp.post("/departments/add")
@super_required
def department_add():
    name = (request.form.get("name") or "").strip()
    if not name:
        flash("يرجى إدخال اسم القسم.", "error")
    elif db.department_exists(name):
        flash(f"القسم «{name}» موجود مسبقاً.", "error")
    else:
        db.add_department(name)
        _audit("department_add", target=name)
        flash(f"تمت إضافة القسم «{name}».", "success")
    return redirect(url_for("admin.admins"))


@bp.post("/departments/<int:dept_id>/delete")
@super_required
def department_delete(dept_id: int):
    dept = db.get_department(dept_id)
    if not dept:
        flash("القسم غير موجود.", "error")
    else:
        db.delete_department(dept_id)
        _audit("department_delete", target=dept["name"])
        flash(f"تم حذف القسم «{dept['name']}». المشرفون والمستندات المرتبطون به لم يُحذفوا.",
              "success")
    return redirect(url_for("admin.admins"))


@bp.post("/admins/add")
@super_required
def admin_add():
    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""
    department = (request.form.get("department") or "").strip()
    if not _USERNAME_RE.match(username):
        flash("اسم المستخدم: 3–32 حرفاً لاتينياً/أرقاماً (يبدأ بحرف أو رقم؛ يُسمح بـ . _ -).", "error")
        return redirect(url_for("admin.admins"))
    if len(password) < MIN_PASSWORD_LEN:
        flash(f"كلمة المرور يجب ألا تقل عن {MIN_PASSWORD_LEN} أحرف.", "error")
        return redirect(url_for("admin.admins"))
    if not department or not db.department_exists(department):
        flash("يرجى اختيار قسم من القائمة. أضِف الأقسام أولاً إن لم تكن موجودة.", "error")
        return redirect(url_for("admin.admins"))
    if db.get_admin_by_username(username):
        flash("اسم المستخدم موجود مسبقاً.", "error")
        return redirect(url_for("admin.admins"))
    me = current_admin() or {}
    db.create_admin(username, hash_password(password), role="admin",
                    created_by=me.get("username", ""), department=department)
    _audit("admin_create", target=username, detail=f"القسم: {department}")
    flash(f"تمت إضافة المشرف «{username}» لقسم «{department}».", "success")
    return redirect(url_for("admin.admins"))


@bp.post("/admins/<int:admin_id>/delete")
@super_required
def admin_delete(admin_id: int):
    target = db.get_admin(admin_id)
    me = current_admin() or {}
    if not target:
        flash("المشرف غير موجود.", "error")
    elif target["id"] == me.get("id"):
        flash("لا يمكنك حذف حسابك الحالي.", "error")
    elif target.get("role") == "super":
        flash("لا يمكن حذف المشرف الرئيسي.", "error")
    else:
        db.delete_admin(admin_id)
        _audit("admin_delete", target=target["username"])
        flash(f"تم حذف المشرف «{target['username']}». سجل نشاطه محفوظ.", "success")
    return redirect(url_for("admin.admins"))


@bp.post("/admins/<int:admin_id>/department")
@super_required
def admin_set_department(admin_id: int):
    target = db.get_admin(admin_id)
    if not target:
        flash("المشرف غير موجود.", "error")
    elif target.get("role") == "super":
        flash("المشرف الرئيسي يرى كل الأقسام.", "error")
    else:
        dept = (request.form.get("department") or "").strip()
        if dept and not db.department_exists(dept):
            flash("القسم المحدّد غير موجود في القائمة.", "error")
            return redirect(url_for("admin.admins"))
        db.update_admin_department(admin_id, dept)
        _audit("admin_set_department", target=target["username"], detail=dept or "—")
        flash(f"تم تحديث قسم «{target['username']}».", "success")
    return redirect(url_for("admin.admins"))


@bp.post("/admins/<int:admin_id>/password")
@super_required
def admin_reset_password(admin_id: int):
    target = db.get_admin(admin_id)
    new = request.form.get("new_password") or ""
    me = current_admin() or {}
    if not target:
        flash("المشرف غير موجود.", "error")
    elif target.get("role") == "super" and target["id"] != me.get("id"):
        flash("لا يمكن تغيير كلمة مرور المشرف الرئيسي من هنا.", "error")
    elif len(new) < MIN_PASSWORD_LEN:
        flash(f"كلمة المرور يجب ألا تقل عن {MIN_PASSWORD_LEN} أحرف.", "error")
    else:
        db.update_admin_password(admin_id, hash_password(new))
        _audit("admin_reset_password", target=target["username"])
        flash(f"تم تحديث كلمة مرور «{target['username']}».", "success")
    return redirect(url_for("admin.admins"))


# --------------------------------------------------------------------------- #
#  Audit trail (super admin only)                                             #
# --------------------------------------------------------------------------- #
@bp.get("/audit")
@super_required
def audit():
    page = max(1, int(request.args.get("page", 1)))
    per = 30
    username = request.args.get("user", "")
    rows, total = db.list_audit(per, (page - 1) * per, username)
    pages = max(1, math.ceil(total / per))
    return render_template("admin/audit.html", entries=rows, page=page, pages=pages,
                           total=total, user=username, usernames=db.audit_usernames())


# --------------------------------------------------------------------------- #
#  Analytics / insights                                                       #
# --------------------------------------------------------------------------- #
def _top_similarity(sources_json: str) -> float:
    try:
        srcs = json.loads(sources_json or "[]")
    except (json.JSONDecodeError, TypeError):
        return 0.0
    sims = [s.get("similarity") or 0 for s in srcs if isinstance(s, dict)]
    return max(sims) if sims else 0.0


@bp.get("/analytics")
@login_required
def analytics():
    from ...core.utils import normalize_text

    stats = db.get_dashboard_stats()
    fb = db.chat_feedback_stats()
    satisfaction = round(fb["up"] / fb["rated"] * 100) if fb["rated"] else None
    served = db.served_breakdown()
    total_chats = sum(s["n"] for s in served) or 1
    from_memory = next((s["n"] for s in served if s["served_from"] == "memory"), 0)
    memory_pct = round(from_memory / total_chats * 100)
    langs = db.lang_breakdown()

    # Per-day series (oldest→newest), gap-filled.
    from datetime import date, timedelta
    raw_days = {d["day"]: d["n"] for d in db.chats_per_day(14)}
    base = date.fromisoformat(now_iso()[:10])
    series = []
    for i in range(13, -1, -1):
        d = (base - timedelta(days=i)).isoformat()
        series.append({"day": d[5:], "n": raw_days.get(d, 0)})
    day_max = max((d["n"] for d in series), default=0) or 1

    # Top + gap questions from recent answers.
    logs = db.recent_rag_logs(600)
    counts: dict[str, dict] = {}
    gaps: dict[str, dict] = {}
    for r in logs:
        q = (r["question"] or "").strip()
        if not q:
            continue
        key = normalize_text(q)
        c = counts.setdefault(key, {"question": q, "n": 0, "down": 0})
        c["n"] += 1
        if r["feedback"] == -1:
            c["down"] += 1
        # A "gap" = a RAG answer with no/weak supporting source.
        if r["served_from"] == "rag" and _top_similarity(r["sources_json"]) < 0.45:
            g = gaps.setdefault(key, {"question": q, "n": 0})
            g["n"] += 1
    top = sorted(counts.values(), key=lambda x: x["n"], reverse=True)[:15]
    gap = sorted(gaps.values(), key=lambda x: x["n"], reverse=True)[:15]

    return render_template(
        "admin/analytics.html", stats=stats, fb=fb, satisfaction=satisfaction,
        served=served, memory_pct=memory_pct, langs=langs, series=series,
        day_max=day_max, top=top, gap=gap,
        served_ar={"rag": "بحث وتوليد", "memory": "ذاكرة", "limit": "حد الجلسة",
                   "budget": "سقف الميزانية", "error": "خطأ", "rate_limited": "تقييد المعدّل",
                   "web": "بحث في الموقع"},
    )


# --------------------------------------------------------------------------- #
#  Token usage & cost                                                         #
# --------------------------------------------------------------------------- #
@bp.get("/costs")
@login_required
def costs():
    pricing = tokens.get_pricing()

    by_model: dict[str, dict] = {}
    by_op: dict[str, dict] = {}
    total_cost = total_p = total_c = 0.0
    for r in db.token_breakdown():
        total_cost += r["cost"]
        total_p += r["prompt"]
        total_c += r["completion"]
        m = by_model.setdefault(r["model"], {
            "model": r["model"], "prompt": 0, "completion": 0, "calls": 0, "cost": 0.0,
            "priced": r["model"] in pricing})
        m["prompt"] += r["prompt"]; m["completion"] += r["completion"]
        m["calls"] += r["calls"]; m["cost"] += r["cost"]
        o = by_op.setdefault(r["operation"], {
            "operation": r["operation"],
            "label": tokens.OPERATION_LABELS.get(r["operation"], r["operation"]),
            "prompt": 0, "completion": 0, "calls": 0, "cost": 0.0})
        o["prompt"] += r["prompt"]; o["completion"] += r["completion"]
        o["calls"] += r["calls"]; o["cost"] += r["cost"]

    today = db.token_totals(today_only=True)
    totals = db.token_totals()

    docs = [{
        "doc_id": r["doc_id"], "filename": r["filename"] or f"#{r['doc_id']}",
        "file_type": r["file_type"], "prompt": r["prompt"],
        "completion": r["completion"], "cost": r["cost"], "estimated": r["estimated"],
    } for r in db.token_by_document()]

    budget_raw = db.get_setting("monthly_budget_usd")
    try:
        monthly_budget = float(budget_raw) if budget_raw not in (None, "") \
            else settings.MONTHLY_BUDGET_USD
    except (TypeError, ValueError):
        monthly_budget = settings.MONTHLY_BUDGET_USD
    month_cost = db.token_month_cost()

    return render_template(
        "admin/costs.html",
        pricing=pricing,
        by_model=sorted(by_model.values(), key=lambda x: x["cost"], reverse=True),
        by_op=sorted(by_op.values(), key=lambda x: x["cost"], reverse=True),
        docs=docs,
        total_cost=total_cost, total_prompt=total_p, total_completion=total_c,
        today_cost=today["cost"], today_tokens=today["prompt"] + today["completion"],
        has_estimates=totals["estimated_calls"] > 0,
        monthly_budget=monthly_budget, month_cost=month_cost,
        budget_pct=(min(100, round(month_cost / monthly_budget * 100)) if monthly_budget > 0 else 0),
        can_edit=is_super(),
    )


@bp.post("/costs/budget")
@super_required
def costs_budget():
    try:
        val = max(0.0, float(request.form.get("monthly_budget_usd", 0) or 0))
        db.set_setting("monthly_budget_usd", str(val))
        _audit("budget_set", detail=f"${val:.2f}/شهر" if val else "بلا حد")
        flash("تم تحديث سقف الإنفاق الشهري." if val else "تم إلغاء سقف الإنفاق (بلا حد).",
              "success")
    except (TypeError, ValueError):
        flash("القيمة يجب أن تكون رقماً.", "error")
    return redirect(url_for("admin.costs"))


@bp.post("/costs/refresh-prices")
@super_required
def costs_refresh_prices():
    try:
        live = tokens.refresh_pricing_from_openrouter()
        _audit("pricing_refresh", detail=f"{len(live)} نموذج من OpenRouter")
        flash(f"تم تحديث الأسعار من OpenRouter ({len(live)} نموذج).", "success")
    except Exception as exc:  # noqa: BLE001
        log.exception("Price refresh failed")
        flash(f"تعذّر جلب الأسعار من OpenRouter: {exc}", "error")
    return redirect(url_for("admin.costs"))


@bp.post("/costs/pricing")
@super_required
def costs_pricing():
    models = request.form.getlist("model")
    inputs = request.form.getlist("input")
    outputs = request.form.getlist("output")
    pricing: dict[str, dict] = {}
    for m, i, o in zip(models, inputs, outputs):
        m = (m or "").strip()
        if not m:
            continue
        try:
            pricing[m] = {"input": max(0.0, float(i or 0)), "output": max(0.0, float(o or 0))}
        except (TypeError, ValueError):
            continue
    tokens.save_pricing(pricing)
    _audit("pricing_update", detail=f"{len(pricing)} نموذج")
    flash("تم تحديث أسعار الرموز (لكل مليون رمز).", "success")
    return redirect(url_for("admin.costs"))


# --------------------------------------------------------------------------- #
#  Settings                                                                   #
# --------------------------------------------------------------------------- #
@bp.get("/settings")
@login_required
def settings_page():
    chat_limit = db.get_int_setting("chat_limit", settings.CHAT_LIMIT_PER_SESSION)
    web_raw = db.get_setting("web_search_enabled")
    web_on = (web_raw.strip().lower() in {"1", "true", "yes", "on"}
              if web_raw is not None else settings.WEB_SEARCH_ENABLED)
    return render_template("admin/settings.html", chat_limit=chat_limit, cfg=settings,
                           web_on=web_on, tts_ar_provider=speech.arabic_provider())


@bp.post("/settings/speech")
@login_required
def settings_speech():
    provider = request.form.get("tts_ar_provider")
    if provider not in {"elevenlabs", "fish", "fish_male"}:
        return jsonify(error="Invalid Arabic speech provider"), 400
    db.set_setting("tts_ar_provider", provider)
    _audit("settings_change", target="tts_ar_provider", detail=provider)
    english = request.cookies.get("ju_admin_lang") == "en"
    flash("Arabic voice provider saved." if english else "تم حفظ مزوّد الصوت العربي.", "success")
    return redirect(url_for("admin.settings_page"))


@bp.post("/settings/web-search")
@login_required
def settings_web_search():
    on = (request.form.get("web_search_enabled") == "on")
    db.set_setting("web_search_enabled", "true" if on else "false")
    _audit("settings_change", target="web_search", detail="مُفعّل" if on else "مُعطّل")
    flash("تم تفعيل البحث في موقع الجامعة." if on else "تم تعطيل البحث في موقع الجامعة.",
          "success")
    return redirect(url_for("admin.settings_page"))


@bp.post("/settings")
@login_required
def settings_save():
    try:
        value = int(request.form.get("chat_limit", settings.CHAT_LIMIT_PER_SESSION))
        value = max(1, min(value, 100000))
        db.set_setting("chat_limit", str(value))
        _audit("settings_change", target="chat_limit", detail=str(value))
        flash(f"تم ضبط حدّ الرسائل لكل جلسة على {value}.", "success")
    except (TypeError, ValueError):
        flash("يجب أن يكون الحدّ رقماً صحيحاً.", "error")
    return redirect(url_for("admin.settings_page"))


@bp.post("/settings/password")
@login_required
def change_password():
    admin = current_admin()
    current = request.form.get("current_password") or ""
    new = request.form.get("new_password") or ""
    if not admin or not verify_password(admin["password_hash"], current):
        flash("كلمة المرور الحالية غير صحيحة.", "error")
    elif len(new) < MIN_PASSWORD_LEN:
        flash(f"يجب ألا تقل كلمة المرور الجديدة عن {MIN_PASSWORD_LEN} أحرف.", "error")
    else:
        db.update_admin_password(admin["id"], hash_password(new))
        _audit("change_own_password", target=admin["username"])
        flash("تم تحديث كلمة المرور.", "success")
    return redirect(url_for("admin.settings_page"))

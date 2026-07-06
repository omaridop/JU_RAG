"""Public chat blueprint: the student-facing assistant + its JSON/SSE API."""

from __future__ import annotations

import json
from uuid import uuid4

from flask import (Blueprint, Response, jsonify, render_template, request,
                   session, stream_with_context)

from ...config import settings
from ...core import db
from ...core.utils import detect_lang
from ...chatbot import rag, prompts
from ..ratelimit import allow, client_ip

bp = Blueprint("chat", __name__)


def _session_token() -> str:
    tok = session.get("chat_token")
    if not tok:
        tok = uuid4().hex
        session["chat_token"] = tok
        session.permanent = True
    db.get_or_create_session(tok)
    return tok


def _limit_state(tok: str) -> dict:
    used = db.session_message_count(tok)
    limit = db.get_int_setting("chat_limit", settings.CHAT_LIMIT_PER_SESSION)
    return {"used": used, "limit": limit, "remaining": max(0, limit - used)}


def _answer_style(data: dict) -> str:
    """Per-message answer length chosen by the student ('concise' | 'detailed')."""
    return "detailed" if (data.get("answer_style") == "detailed") else "concise"


@bp.get("/")
def index():
    tok = _session_token()
    return render_template("chat.html", limit=_limit_state(tok),
                           web_available=rag._web_enabled())


@bp.get("/api/limit")
def api_limit():
    return jsonify(_limit_state(_session_token()))


@bp.post("/api/reset")
def api_reset():
    session["chat_token"] = uuid4().hex
    db.get_or_create_session(session["chat_token"])
    return jsonify({"ok": True, **_limit_state(session["chat_token"])})


@bp.post("/api/feedback")
def api_feedback():
    """Record a like (1) or dislike (-1) on a specific answer."""
    data = request.get_json(silent=True) or {}
    try:
        log_id = int(data.get("log_id"))
        value = int(data.get("value"))
    except (TypeError, ValueError):
        return jsonify({"error": "bad request"}), 400
    db.set_feedback(log_id, value)
    return jsonify({"ok": True})


@bp.post("/api/chat")
def api_chat():
    """Blocking, non-streaming answer (fallback for clients without streaming)."""
    data = request.get_json(silent=True) or {}
    message = (data.get("message") or "").strip()
    if not message:
        return jsonify({"error": "empty message"}), 400
    if not allow(client_ip()):
        lang = detect_lang(message)
        return jsonify({"answer": prompts.rate_limit_message(lang), "sources": [],
                        "served_from": "rate_limited", "lang": lang}), 429
    tok = _session_token()
    result = rag.generate_answer(message, tok, use_web=bool(data.get("web_search")),
                                 answer_style=_answer_style(data))
    result["limit"] = _limit_state(tok)
    return jsonify(result)


@bp.post("/api/chat/stream")
def api_chat_stream():
    """Server-Sent-Events stream of the answer (consumed via fetch())."""
    data = request.get_json(silent=True) or {}
    message = (data.get("message") or "").strip()
    use_web = bool(data.get("web_search"))
    style = _answer_style(data)
    rate_ok = allow(client_ip()) if message else True
    tok = _session_token()

    def gen():
        if not message:
            yield f"data: {json.dumps({'type': 'error', 'text': 'empty message'})}\n\n"
            return
        if not rate_ok:
            lang = detect_lang(message)
            yield f"data: {json.dumps({'type': 'meta', 'lang': lang, 'served_from': 'rate_limited'})}\n\n"
            yield f"data: {json.dumps({'type': 'delta', 'text': prompts.rate_limit_message(lang)}, ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps({'type': 'done', 'served_from': 'rate_limited', 'sources': []})}\n\n"
            return
        for event in rag.stream_answer(message, tok, use_web=use_web, answer_style=style):
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        # Trailing limit snapshot so the UI can update its counter.
        yield f"data: {json.dumps({'type': 'limit', **_limit_state(tok)})}\n\n"

    return Response(
        stream_with_context(gen()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                 "Connection": "keep-alive"},
    )

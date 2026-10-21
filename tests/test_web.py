"""Offline route/security smoke tests using Flask's test client.

These exercise routing, CSRF, security headers and rate limiting WITHOUT making
any model calls (we never hit /api/chat with a real message that reaches the LLM
beyond the rate-limit / empty-message guards)."""
import pytest
import json
from wsgiref.util import is_hop_by_hop

from app.web import create_app


@pytest.fixture()
def client():
    app = create_app()
    app.config.update(TESTING=True)
    return app.test_client()


def test_healthz_ok(client):
    r = client.get("/healthz")
    assert r.status_code in (200, 503)
    assert "status" in r.get_json()


def test_security_headers_present(client):
    r = client.get("/")
    assert r.headers.get("X-Content-Type-Options") == "nosniff"
    assert r.headers.get("X-Frame-Options") == "SAMEORIGIN"
    assert "Content-Security-Policy" in r.headers


def test_login_page_renders_with_csrf(client):
    r = client.get("/admin/login")
    assert r.status_code == 200
    assert b"csrf_token" in r.data


def test_admin_requires_login(client):
    r = client.get("/admin/", follow_redirects=False)
    assert r.status_code in (301, 302)
    assert "/admin/login" in r.headers["Location"]


def test_post_without_csrf_is_rejected(client):
    r = client.post("/admin/login", data={"username": "admin", "password": "x"})
    assert r.status_code == 400      # CSRF guard blocks tokenless POST


def test_empty_chat_message_rejected(client):
    r = client.post("/api/chat", json={"message": ""})
    assert r.status_code == 400


def test_chat_stream_is_wsgi_safe_and_completes(client, monkeypatch):
    import app.web.routes.chat as chatmod
    events = [{"type": "delta", "text": "Hello"},
              {"type": "done", "sources": [], "served_from": "rag"}]
    monkeypatch.setattr(chatmod, "allow", lambda ip: True)
    monkeypatch.setattr(chatmod.rag, "stream_answer", lambda *args, **kwargs: iter(events))
    response = client.post("/api/chat/stream", json={"message": "hello"})
    assert response.status_code == 200
    assert response.mimetype == "text/event-stream"
    assert not any(is_hop_by_hop(name) for name in response.headers.keys())
    assert response.headers["X-Accel-Buffering"] == "no"
    received = [json.loads(part.removeprefix("data: "))
                for part in response.get_data(as_text=True).strip().split("\n\n")]
    assert received[:2] == events
    assert received[-1]["type"] == "limit"


def test_rate_limiter_unit_trips_and_resets(monkeypatch):
    """Pure limiter logic — no Flask, no network."""
    from app.web import ratelimit
    monkeypatch.setattr(ratelimit, "_hits", __import__("collections").defaultdict(
        __import__("collections").deque))
    monkeypatch.setattr(ratelimit.settings, "RATE_LIMIT_PER_MIN", 3, raising=False)
    monkeypatch.setattr(ratelimit.settings, "RATE_LIMIT_PER_HOUR", 0, raising=False)
    monkeypatch.setattr(ratelimit.settings, "RATE_LIMIT_PER_DAY", 0, raising=False)
    ip = "1.2.3.4"
    assert [ratelimit.allow(ip) for _ in range(5)] == [True, True, True, False, False]
    assert ratelimit.allow("9.9.9.9") is True   # a different IP is unaffected


def test_chat_route_returns_429_when_limited(client, monkeypatch):
    """Route returns 429 once limited — generator stubbed so no LLM is called."""
    import app.web.routes.chat as chatmod
    monkeypatch.setattr(chatmod.rag, "generate_answer",
                        lambda msg, tok, use_web=False: {"answer": "stub", "sources": [],
                                                         "served_from": "rag", "lang": "en"})
    monkeypatch.setattr(chatmod, "allow", lambda ip: False)  # force limited
    r = client.post("/api/chat", json={"message": "hi"})
    assert r.status_code == 429
    assert r.get_json()["served_from"] == "rate_limited"


def test_speech_settings_require_csrf(client):
    assert client.post('/admin/settings/speech', data={'tts_ar_provider': 'fish'}).status_code == 400


def test_speech_settings_require_login(client):
    with client.session_transaction() as session:
        session['_csrf'] = 'test-token'
    response = client.post('/admin/settings/speech', data={'csrf_token': 'test-token', 'tts_ar_provider': 'fish'})
    assert response.status_code == 302
    assert '/admin/login' in response.headers['Location']


def test_speech_settings_validate_save_and_audit(client, monkeypatch):
    from app.core import auth, db
    from app.web.routes import admin
    monkeypatch.setattr(auth, 'current_admin', lambda: {'id': 1, 'username': 'test', 'role': 'super'})
    audits = []
    monkeypatch.setattr(admin, '_audit', lambda *args, **kwargs: audits.append((args, kwargs)))
    with client.session_transaction() as session:
        session['_csrf'] = 'test-token'
    for value in ['fish', 'elevenlabs']:
        response = client.post('/admin/settings/speech', data={'csrf_token': 'test-token', 'tts_ar_provider': value})
        assert response.status_code == 302
        assert db.get_setting('tts_ar_provider') == value
    assert len(audits) == 2
    response = client.post('/admin/settings/speech', data={'csrf_token': 'test-token', 'tts_ar_provider': 'custom'})
    assert response.status_code == 400
    assert db.get_setting('tts_ar_provider') == 'elevenlabs'
    assert len(audits) == 2
    response = client.get('/admin/settings')
    assert response.status_code == 200
    assert b'Fish Audio' in response.data
    assert b'for="tts_ar_provider"' in response.data

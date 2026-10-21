"""Offline voice-mode contracts; never open the live vector store."""
import io
import base64
import httpx
import pytest
from flask import Flask
from app.chatbot import transcription, prompts, rag, speech
from app.web.routes import chat


def test_voice_persona_and_short_cap():
    note = prompts.style_note("voice")
    assert "أحمد الزعبي" in note and "Jordanian" in note
    assert rag._voice_token_limit([{"role": "system", "content": note}]) <= 320
    assert "الفصحى" not in prompts.build_user_message("مرحبا", "", "ar", "voice")
    assert chat._answer_style({"voice_mode": True}) == "voice"


@pytest.mark.parametrize("audio,fmt", [(b"", "webm"), (b"x", "exe"), (b"x" * 1000001, "webm")], ids=["empty", "format", "oversized"])
def test_invalid_audio_rejected(audio, fmt):
    with pytest.raises(speech.SpeechError):
        transcription.transcribe(audio, fmt, "test")


def test_transcription_schema_and_accounting(monkeypatch):
    calls, usage = [], []
    def post(url, **kw):
        calls.append(kw["json"])
        return httpx.Response(200, json={"text": "مرحبا", "usage": {"cost": 0.001}}, request=httpx.Request("POST", url))
    monkeypatch.setattr(transcription.httpx, "post", post)
    monkeypatch.setattr(transcription.db, "add_token_usage", lambda **kw: usage.append(kw))
    assert transcription.transcribe(b"test", "webm", "test") == "مرحبا"
    assert calls[0]["model"] == "elevenlabs/scribe-v2"
    assert base64.b64decode(calls[0]["input_audio"]["data"]) == b"test"
    assert usage[0]["cost_usd"] == 0.001 and usage[0]["estimated"] == 0


def test_transcription_route_guards(monkeypatch):
    app = Flask(__name__); app.secret_key = "test"; app.register_blueprint(chat.bp)
    monkeypatch.setattr(chat.settings, "TTS_ENABLED", True)
    monkeypatch.setattr(chat, "allow", lambda _: True)
    monkeypatch.setattr(chat, "_limit_state", lambda _: {"remaining": 1})
    monkeypatch.setattr(chat.rag, "_monthly_budget", lambda: 0)
    monkeypatch.setattr(transcription, "transcribe", lambda *args: "مرحبا")
    client = app.test_client()
    assert client.post('/api/voice/transcribe').status_code == 403
    with client.session_transaction() as session:
        session['_csrf'] = 'test'; session['chat_token'] = 'owner'
    headers = {'X-CSRF-Token': 'test'}
    assert client.post('/api/voice/transcribe',headers=headers,data={'audio':(io.BytesIO(b'audio'),'test.webm','audio/webm')}).json == {'text':'مرحبا'}
    monkeypatch.setattr(chat, "_limit_state", lambda _: {"remaining": 0})
    assert client.post('/api/voice/transcribe',headers=headers,data={'audio':(io.BytesIO(b'audio'),'test.webm')}).status_code == 403

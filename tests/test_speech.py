"""Offline speech tests: no real database, vector store, or provider calls."""
import httpx
import pytest
from types import SimpleNamespace
from flask import Flask

from app.chatbot import speech
from app.web.routes import chat


@pytest.fixture
def speech_env(monkeypatch):
    speech._cache.clear()
    calls, usage = [], []
    monkeypatch.setattr(speech.settings, "TTS_ENABLED", True)
    monkeypatch.setattr(speech.settings, "TTS_USD_PER_CHARACTER", 0.00004)
    monkeypatch.setattr(speech.db, "get_setting", lambda key: "0")
    monkeypatch.setattr(speech.db, "token_month_cost", lambda: 0)
    monkeypatch.setattr(speech.db, "add_token_usage", lambda **kw: usage.append(kw))

    def post(url, **kwargs):
        calls.append(kwargs)
        return httpx.Response(200, headers={"content-type": "audio/mpeg"},
                              content=b"ID3-test-audio", request=httpx.Request("POST", url))

    monkeypatch.setattr(speech.httpx, "post", post)
    yield calls, usage
    speech._cache.clear()


def test_generation_uses_selected_voice_and_caches(speech_env):
    calls, usage = speech_env
    assert speech.generate("Hello **student**", "owner") == b"ID3-test-audio"
    assert speech.generate("Hello **student**", "owner") == b"ID3-test-audio"
    assert len(calls) == len(usage) == 1
    assert calls[0]["json"] == {"model": speech.settings.TTS_MODEL,
                                 "voice": "4wf10lgibMnboGJGCLrP",
                                 "input": "Hello student", "response_format": "mp3"}
    assert usage[0]["prompt_tokens"] == 0
    assert usage[0]["estimated"] == 1
    assert usage[0]["cost_usd"] == pytest.approx(13 * 0.00004)


def test_budget_blocks_provider_call(speech_env, monkeypatch):
    monkeypatch.setattr(speech.db, "get_setting", lambda key: "0.00001")
    with pytest.raises(speech.SpeechError, match="") as exc:
        speech.generate("Hello", "owner")
    assert exc.value.code == "budget"
    assert not speech_env[0]


@pytest.mark.parametrize("answer", ["", "x" * 10001])
def test_no_silent_truncation(speech_env, answer):
    with pytest.raises(speech.SpeechError) as exc:
        speech.generate(answer, "owner")
    assert exc.value.code == "length"
    assert not speech_env[0]


def test_provider_failure_can_be_retried(speech_env, monkeypatch):
    def fail(*args, **kwargs):
        raise httpx.ReadTimeout("upstream")
    monkeypatch.setattr(speech.httpx, "post", fail)
    with pytest.raises(speech.SpeechError) as exc:
        speech.generate("Hello", "owner")
    assert exc.value.code == "unavailable"
    assert not speech_env[1]
    assert not speech._lock.locked()


def test_parallel_generation_rejected(speech_env):
    with speech._lock:
        with pytest.raises(speech.SpeechError) as exc:
            speech.generate("Hello", "owner")
    assert exc.value.status == 429


def test_markdown_cleanup_preserves_answer():
    assert speech.spoken_text("## أهلاً\n**رسوم** [التسجيل](https://ju.edu.jo) 42 دينار [1]") == "أهلاً رسوم التسجيل 42 دينار"


@pytest.fixture
def api(speech_env, monkeypatch):
    app = Flask(__name__)
    app.config.update(TESTING=True, SECRET_KEY="test-only")
    app.register_blueprint(chat.bp)
    monkeypatch.setattr(chat, "allow", lambda ip: True)
    monkeypatch.setattr(chat.db, "get_chat_log", lambda log_id: {
        "session_token": "owner", "answer": "Hello", "served_from": "rag"
    } if log_id == 7 else None)
    client = app.test_client()
    with client.session_transaction() as session:
        session["chat_token"] = "owner"
    return client


def test_route_returns_private_audio(api):
    response = api.post("/api/speech", json={"log_id": 7, "text": "Do not read client text"})
    assert response.status_code == 200
    assert response.mimetype == "audio/mpeg"
    assert response.headers["Cache-Control"] == "private, no-store"


def test_other_sessions_cannot_read_answer(api, speech_env):
    with api.session_transaction() as session:
        session["chat_token"] = "stranger"
    assert api.post("/api/speech", json={"log_id": 7}).status_code == 404
    assert not speech_env[0]


@pytest.mark.parametrize("payload", [{}, [], {"log_id": True}, {"log_id": "7"}])
def test_invalid_input(api, payload):
    assert api.post("/api/speech", json=payload).status_code == 400


def test_rate_limit(api, monkeypatch, speech_env):
    monkeypatch.setattr(chat, "allow", lambda ip: False)
    assert api.post("/api/speech", json={"log_id": 7}).status_code == 429
    assert not speech_env[0]


def test_feature_switch(api, monkeypatch):
    monkeypatch.setattr(chat.settings, "TTS_ENABLED", False)
    assert api.post("/api/speech", json={"log_id": 7}).status_code == 503


def fake_diacritics(monkeypatch, output, finish="stop"):
    calls = []
    def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=output), finish_reason=finish)],
            usage={"prompt_tokens": 100, "completion_tokens": 20, "cost": 0.0001})
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    client.with_options = lambda **kwargs: client
    monkeypatch.setattr(speech, "get_client", lambda: client)
    return calls


def test_diacritics_before_audio_and_cached(speech_env, monkeypatch):
    output = "مَرْحَباً بِكُمْ في الجامعة."
    preparation = fake_diacritics(monkeypatch, output)
    speech.generate("مرحباً بكم في الجامعة.", "owner")
    speech.generate("مرحباً بكم في الجامعة.", "owner")
    calls, usage = speech_env
    assert len(preparation) == len(calls) == 1
    assert calls[0]["json"]["input"] == output
    assert [entry["operation"] for entry in usage] == ["speech_diacritics", "speech"]
    assert usage[0]["session_token"] == "owner"
    assert usage[1]["cost_usd"] == pytest.approx(len(output) * 0.00004)


@pytest.mark.parametrize("output,finish", [
    ("أهلاً بكم في الجامعة.", "stop"),  # paraphrase
    ("مَرْحَباً", "length"),  # truncated
    ("مرحباً بكم في الجامعة. شرح إضافي", "stop"),
    ("", "stop"),
    ("الرُّسومُ 50 ديناراً.", "stop"),  # changed number / content
])
def test_unsafe_diacritics_fall_back(speech_env, monkeypatch, output, finish):
    fake_diacritics(monkeypatch, output, finish)
    original = "مرحباً بكم في الجامعة."
    speech.generate(original, "owner")
    assert speech_env[0][0]["json"]["input"] == original


def test_english_skips_diacritics(speech_env, monkeypatch):
    preparation = fake_diacritics(monkeypatch, "unexpected")
    speech.generate("Hello students", "owner")
    assert not preparation


def test_diacritics_timeout_falls_back(speech_env, monkeypatch):
    def unavailable():
        raise httpx.ReadTimeout("timeout")
    monkeypatch.setattr(speech, "get_client", unavailable)
    speech.generate("مرحبا", "owner")
    assert speech_env[0][0]["json"]["input"] == "مرحبا"


def test_diacritics_preserve_mixed_text_and_numbers(speech_env, monkeypatch):
    result = "رُسومُ IT هي 42 ديناراً."
    fake_diacritics(monkeypatch, result)
    assert speech.diacritize("رسوم IT هي 42 ديناراً.", "owner") == result

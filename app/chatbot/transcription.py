"""Short voice turns, forwarded in memory; never persist microphone audio."""
import base64
import httpx

from ..config import settings
from ..core import db
from .speech import SpeechError

MODEL = "elevenlabs/scribe-v2"
MAX_BYTES = 1_000_000


def transcribe(audio, fmt, session_token):
    if not audio or len(audio) > MAX_BYTES or fmt not in {"webm", "mp4", "ogg", "wav", "mp3"}:
        raise SpeechError("invalid_audio", 400)
    try:
        response = httpx.post(
            settings.OPENROUTER_BASE_URL.rstrip("/") + "/audio/transcriptions",
            headers={"Authorization": f"Bearer {settings.OPENROUTER_API_KEY}"},
            json={"model": MODEL, "input_audio": {
                "data": base64.b64encode(audio).decode("ascii"),
                "format": "m4a" if fmt == "mp4" else fmt}},
            timeout=httpx.Timeout(65, connect=10))
        response.raise_for_status()
        result = response.json()
        text = result.get("text", "")
        if not isinstance(text, str) or len(text) > 4000:
            raise ValueError("invalid transcript")
        usage = result.get("usage") or {}
        cost = usage.get("cost")
        db.add_token_usage(model=MODEL, operation="transcription", prompt_tokens=0,
                           completion_tokens=0, cost_usd=float(cost) if cost is not None else 0.001834,
                           estimated=int(cost is None), session_token=session_token)
        return text.strip()
    except (httpx.HTTPError, ValueError, TypeError, AttributeError):
        raise SpeechError("transcription_unavailable", 502) from None

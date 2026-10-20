"""On-demand narration of saved answers; never accepts arbitrary client text."""
from collections import OrderedDict
import hashlib
import html
import logging
import re
import threading
import time

import httpx

from ..config import settings
from ..core import db

log = logging.getLogger(__name__)
_lock = threading.Lock()
_cache = OrderedDict()
_CACHE_BYTES = 32 * 1024 * 1024


class SpeechError(Exception):
    def __init__(self, code, status):
        self.code, self.status = code, status


def spoken_text(answer):
    """Keep the answer's wording, without reading Markdown or link targets."""
    text = re.sub(r"!?\[([^\]]+)\]\(https?://[^\s)]+\)", r"\1", answer)
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"(?m)^\s{0,3}(?:#{1,6}\s+|>\s*|[-*+]\s+)", "", text)
    text = re.sub(r"\[\d+(?:[,،\s-]+\d+)*\]", "", text)
    text = re.sub(r"[*_`#\[\]]", "", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def generate(answer, session_token):
    text = spoken_text(answer)
    if not text or len(text) > 10000:
        raise SpeechError("length", 422)
    key = hashlib.sha256((settings.TTS_MODEL + settings.TTS_VOICE + text).encode()).hexdigest()
    # One generation at a time bounds cost/concurrency and prevents duplicate billing.
    if not _lock.acquire(blocking=False):
        raise SpeechError("busy", 429)
    try:
        now = time.monotonic()
        for old in list(_cache):
            if now - _cache[old][0] > 1800:
                del _cache[old]
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key][1]
        estimate = len(text) * max(0, settings.TTS_USD_PER_CHARACTER)
        raw = db.get_setting("monthly_budget_usd")
        try:
            budget = float(raw) if raw is not None else settings.MONTHLY_BUDGET_USD
        except (ValueError, TypeError):
            budget = settings.MONTHLY_BUDGET_USD
        if budget > 0 and db.token_month_cost() + estimate >= budget:
            raise SpeechError("budget", 403)
        try:
            response = httpx.post(
                settings.OPENROUTER_BASE_URL.rstrip("/") + "/audio/speech",
                headers={"Authorization": f"Bearer {settings.OPENROUTER_API_KEY}"},
                json={"model": settings.TTS_MODEL, "voice": settings.TTS_VOICE,
                      "input": text, "response_format": "mp3"},
                timeout=httpx.Timeout(60, connect=10),
            )
            response.raise_for_status()
            if not response.headers.get("content-type", "").startswith("audio/") or not response.content:
                raise SpeechError("unavailable", 502)
        except httpx.HTTPError as exc:
            # Never log provider response bodies (may contain credentials or student text).
            log.warning("Speech request failed (%s)", type(exc).__name__)
            raise SpeechError("unavailable", 502) from None
        # Audio is billed by CHARACTER, not tokens. Keep token totals honest.
        db.add_token_usage(model=settings.TTS_MODEL, operation="speech",
                           prompt_tokens=0, completion_tokens=0, cost_usd=estimate,
                           estimated=1, session_token=session_token)
        audio = response.content
        if len(audio) <= _CACHE_BYTES:
            while _cache and sum(len(v[1]) for v in _cache.values()) + len(audio) > _CACHE_BYTES:
                _cache.popitem(last=False)
            _cache[key] = (time.monotonic(), audio)
        return audio
    finally:
        _lock.release()

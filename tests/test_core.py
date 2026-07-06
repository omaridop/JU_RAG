"""Offline unit tests for the pure logic (no network, no API key needed)."""
from datetime import timedelta

from app.core.utils import (detect_lang, normalize_text, now_utc, parse_dt,
                            recency_score, stable_id, to_iso)


# --- language detection ---------------------------------------------------- #
def test_detect_lang_arabic_vs_english():
    assert detect_lang("ما هي شروط القبول؟") == "ar"
    assert detect_lang("What are the admission requirements?") == "en"
    assert detect_lang("") == "en"
    assert detect_lang("12345 ???") == "en"


# --- text normalization (memory keys) -------------------------------------- #
def test_normalize_unifies_alef_and_strips_punct():
    assert normalize_text("الأردنية!") == normalize_text("الاردنيه")
    assert normalize_text("  Hello,  World!! ") == "hello world"


# --- dates & recency ------------------------------------------------------- #
def test_parse_dt_and_to_iso():
    assert parse_dt("") is None
    assert parse_dt("2026-06-01").year == 2026
    assert to_iso("2026-06-01T10:30").startswith("2026-06-01")


def test_recency_score_decays_with_age():
    now = now_utc()
    fresh = recency_score(now, halflife_days=365, now=now)
    old = recency_score(now - timedelta(days=365), halflife_days=365, now=now)
    assert fresh > old
    assert abs(old - 0.5) < 0.01            # one half-life ≈ 0.5
    assert recency_score(None) == 0.5       # unknown date = neutral


# --- stable ids ------------------------------------------------------------ #
def test_stable_id_is_deterministic():
    assert stable_id("a", "b", "1") == stable_id("a", "b", "1")
    assert stable_id("a", "b", "1") != stable_id("a", "b", "2")

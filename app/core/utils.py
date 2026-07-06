"""Shared helpers: dates, language detection, text normalization, hashing.

Kept dependency-free (stdlib only) so every layer can import it cheaply.
Arabic ranges use explicit \\u escapes to avoid source-encoding fragility.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# --------------------------------------------------------------------------- #
#  Dates (everything is stored as ISO-8601 UTC strings)                       #
# --------------------------------------------------------------------------- #
def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return now_utc().isoformat()


def parse_dt(value: Optional[str | datetime]) -> Optional[datetime]:
    """Parse an ISO date/datetime (or HTML datetime-local) into aware UTC.

    Returns ``None`` if the value is empty/unparseable. Naive inputs are
    assumed to be UTC.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        s = str(value).strip().replace("Z", "+00:00")
        dt = None
        # Try fromisoformat first (handles "2026-06-07", "2026-06-07T14:30", offsets).
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y/%m/%d"):
                try:
                    dt = datetime.strptime(s, fmt)
                    break
                except ValueError:
                    continue
        if dt is None:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def to_iso(value: Optional[str | datetime]) -> Optional[str]:
    dt = parse_dt(value)
    return dt.isoformat() if dt else None


def recency_score(doc_dt: Optional[datetime], halflife_days: float = 365.0,
                  now: Optional[datetime] = None) -> float:
    """Map a document's effective date to a (0, 1] recency weight.

    Newest content scores ~1.0; the score halves every ``halflife_days``. An
    unknown date returns a neutral 0.5 so it is neither boosted nor buried.
    """
    if doc_dt is None:
        return 0.5
    now = now or now_utc()
    if doc_dt.tzinfo is None:
        doc_dt = doc_dt.replace(tzinfo=timezone.utc)
    age_days = max(0.0, (now - doc_dt).total_seconds() / 86400.0)
    halflife = max(1.0, halflife_days)
    return float(0.5 ** (age_days / halflife))


# --------------------------------------------------------------------------- #
#  Language detection (Arabic vs. Latin)                                      #
# --------------------------------------------------------------------------- #
_ARABIC_RE = re.compile(
    "[؀-ۿݐ-ݿࢠ-ࣿﭐ-﷿ﹰ-﻿]"
)
_LATIN_RE = re.compile(r"[A-Za-z]")


def detect_lang(text: str) -> str:
    """Return 'ar' if the text is predominantly Arabic, else 'en'."""
    if not text:
        return "en"
    ar = len(_ARABIC_RE.findall(text))
    la = len(_LATIN_RE.findall(text))
    if ar == 0 and la == 0:
        return "en"
    return "ar" if ar >= la else "en"


def is_arabic(text: str) -> bool:
    return detect_lang(text) == "ar"


# --------------------------------------------------------------------------- #
#  Text normalization (used for exact-match memory keys)                      #
# --------------------------------------------------------------------------- #
# Combining marks/harakat + tatweel. Excludes Arabic-Indic digits (0660-0669)
# so numbers in questions survive normalization.
_DIACRITICS = re.compile("[ؐ-ًؚ-ٰٟـ]")
_PUNCT = re.compile(r"[^\w\s]", flags=re.UNICODE)
_WS = re.compile(r"\s+")

# Arabic letter unifications applied during normalization.
_ALEF = {"أ": "ا", "إ": "ا", "آ": "ا"}  # أ إ آ -> ا
_OTHER = {"ى": "ي", "ة": "ه",                      # ى->ي, ة->ه
          "ؤ": "و", "ئ": "ي"}                      # ؤ->و, ئ->ي
_LETTER_MAP = {**_ALEF, **_OTHER}
_LETTER_TABLE = str.maketrans(_LETTER_MAP)


def normalize_text(text: str) -> str:
    """Aggressively normalize a question for cache-key comparison.

    Lowercases, removes diacritics/tatweel, unifies Arabic letter variants,
    strips punctuation, and collapses whitespace. Two questions that mean the
    same thing but differ in punctuation/diacritics map to the same key.
    """
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", text).strip().lower()
    t = _DIACRITICS.sub("", t)
    t = t.translate(_LETTER_TABLE)
    t = _PUNCT.sub(" ", t)
    t = _WS.sub(" ", t).strip()
    return t


# --------------------------------------------------------------------------- #
#  Hashing & ids                                                              #
# --------------------------------------------------------------------------- #
def stable_id(*parts: str) -> str:
    """Deterministic short id from its parts (idempotent re-ingest)."""
    h = hashlib.sha1("\x1f".join(str(p) for p in parts).encode("utf-8"))
    return h.hexdigest()[:24]


def file_sha256(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(65536), b""):
            h.update(block)
    return h.hexdigest()


def new_uuid() -> str:
    return uuid.uuid4().hex

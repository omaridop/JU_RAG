"""Central configuration.

All settings are read from environment variables (loaded from a ``.env`` file at
the project root) with sensible defaults. Nothing else in the codebase should
read ``os.environ`` directly — import :data:`settings` instead.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# Project root = the directory that contains the ``app`` package.
BASE_DIR = Path(__file__).resolve().parent.parent

# Load .env (if present). Real environment variables always win over the file.
load_dotenv(BASE_DIR / ".env")


# --------------------------------------------------------------------------- #
#  Typed environment helpers                                                  #
# --------------------------------------------------------------------------- #
def _str(key: str, default: str = "") -> str:
    val = os.getenv(key)
    return val if val not in (None, "") else default


def _int(key: str, default: int) -> int:
    try:
        return int(_str(key, str(default)))
    except (TypeError, ValueError):
        return default


def _float(key: str, default: float) -> float:
    try:
        return float(_str(key, str(default)))
    except (TypeError, ValueError):
        return default


def _bool(key: str, default: bool) -> bool:
    return _str(key, str(default)).strip().lower() in {"1", "true", "yes", "on"}


class Config:
    """Application configuration (immutable singleton — see ``settings``)."""

    # --- Filesystem layout ------------------------------------------------- #
    BASE_DIR: Path = BASE_DIR
    DATA_DIR: Path = BASE_DIR / "data"
    UPLOAD_DIR: Path = DATA_DIR / "uploads"
    QA_DIR: Path = DATA_DIR / "qa"
    PROCESSED_DIR: Path = DATA_DIR / "processed"   # JSON chunk artifacts
    CHROMA_DIR: Path = DATA_DIR / "chroma"
    DB_PATH: Path = DATA_DIR / "app.db"

    # --- OpenRouter (single key serves BOTH chat and embeddings) ----------- #
    OPENROUTER_API_KEY: str = _str("OPENROUTER_API_KEY")
    OPENROUTER_BASE_URL: str = _str("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    # Optional headers OpenRouter uses for its public rankings (harmless if kept).
    OPENROUTER_REFERER: str = _str("OPENROUTER_REFERER", "http://localhost:5000")
    OPENROUTER_TITLE: str = _str("OPENROUTER_TITLE", "University of Jordan RAG Assistant")

    # Models
    CHAT_MODEL: str = _str("CHAT_MODEL", "deepseek/deepseek-v4-flash")
    EMBED_MODEL: str = _str("EMBED_MODEL", "google/gemini-embedding-2")
    TTS_MODEL: str = _str("TTS_MODEL", "elevenlabs/eleven-v4-turbo")
    TTS_VOICE: str = _str("TTS_VOICE", "4wf10lgibMnboGJGCLrP")
    TTS_VOICE_EN: str = _str("TTS_VOICE_EN", "XZXyuzPdidXaSa10PmVA")
    TTS_VOICE_SV: str = _str("TTS_VOICE_SV", "aSLKtNoVBZlxQEMsnGL2")
    TTS_ENABLED: bool = _bool("TTS_ENABLED", True)
    TTS_DIACRITICS_MODEL: str = _str("TTS_DIACRITICS_MODEL", CHAT_MODEL)
    TTS_USD_PER_CHARACTER: float = _float("TTS_USD_PER_CHARACTER", 0.00004)
    # The model that READS documents (PDF natively / DOCX text) and chunks them.
    # Must support native PDF input for PDF_INGEST_MODE=native (Gemini does).
    CHUNK_MODEL: str = _str("CHUNK_MODEL", "google/gemini-2.5-flash")
    # 0 => let the provider use its native dimensionality (gemini-embedding-2 -> 3072).
    EMBED_DIM: int = _int("EMBED_DIM", 0)
    EMBED_BATCH: int = _int("EMBED_BATCH", 64)
    # Concurrent embedding requests + per-request timeout (seconds).
    EMBED_CONCURRENCY: int = _int("EMBED_CONCURRENCY", 4)
    EMBED_TIMEOUT_S: int = _int("EMBED_TIMEOUT_S", 90)
    # Hard cap per embedded input; protects against provider token limits.
    EMBED_MAX_CHARS: int = _int("EMBED_MAX_CHARS", 6000)

    # --- Token pricing (USD per 1,000,000 tokens) -------------------------- #
    # Seed values are the REAL OpenRouter rates (verified Jun 2026). They can
    # be refreshed live from OpenRouter's /models API or edited in the admin
    # Cost page (stored in the settings table). Embedding models bill input
    # only (output = 0). For chat/read/QA calls the EXACT cost OpenRouter
    # charges is captured per call and used directly; these rates are the
    # fallback and power the interactive calculator + historical estimates.
    DEFAULT_PRICING: dict = {
        CHAT_MODEL:  {"input": 0.09, "output": 0.18},   # deepseek-v4-flash
        CHUNK_MODEL: {"input": 0.30, "output": 2.50},   # gemini-2.5-flash
        EMBED_MODEL: {"input": 0.20, "output": 0.00},   # gemini-embedding-2
    }

    # --- Timeouts & concurrency --------------------------------------------- #
    # Default client timeout (chat answers are ~1.5k tokens -> quick).
    LLM_TIMEOUT_S: int = _int("LLM_TIMEOUT_S", 120)
    # Ingestion reads/Q&A can emit 16-24k tokens -> need several minutes.
    INGEST_TIMEOUT_S: int = _int("INGEST_TIMEOUT_S", 600)
    # Concurrent page-batch LLM calls during ingestion.
    INGEST_CONCURRENCY: int = _int("INGEST_CONCURRENCY", 4)

    # --- Generation params ------------------------------------------------- #
    CHAT_TEMPERATURE: float = _float("CHAT_TEMPERATURE", 0.2)
    CHAT_MAX_TOKENS: int = _int("CHAT_MAX_TOKENS", 1500)  # headroom for reasoning + answer
    # Budget for rewriting a follow-up into a standalone search query
    # (reasoning models spend part of this thinking).
    CONDENSE_MAX_TOKENS: int = _int("CONDENSE_MAX_TOKENS", 600)
    # Reasoning effort for the chat model (it's a reasoning model — hidden
    # "thinking" tokens are the biggest latency cost). Measured: default ~3.6s
    # to first token, `low` ~1.7s, `off` ~1.3s. A grounded RAG assistant answers
    # from the provided context, so reasoning is unnecessary -> default OFF for
    # max speed. Bump to `low` if complex questions need more care.
    # Values: off | low | medium | high | default (omit to let the model decide).
    CHAT_REASONING_EFFORT: str = _str("CHAT_REASONING_EFFORT", "off")
    # The follow-up query rewrite is mechanical — no thinking needed.
    CONDENSE_REASONING_EFFORT: str = _str("CONDENSE_REASONING_EFFORT", "off")
    # Web-search agent: deciding WHETHER to call the search tool needs a little
    # reasoning (with it off, the model just answers and never searches). Keep a
    # low effort here so auto-search actually fires; simple answers stay off/fast.
    WEB_AGENT_REASONING_EFFORT: str = _str("WEB_AGENT_REASONING_EFFORT", "low")
    CHUNK_TEMPERATURE: float = _float("CHUNK_TEMPERATURE", 0.1)
    CHUNK_MAX_TOKENS: int = _int("CHUNK_MAX_TOKENS", 8000)

    # --- Chunking ---------------------------------------------------------- #
    # Coarse window (characters) of raw text handed to the LLM for semantic
    # chunking. Large enough for context, small enough to stay reliable.
    CHUNK_WINDOW_CHARS: int = _int("CHUNK_WINDOW_CHARS", 12000)
    CHUNK_WINDOW_OVERLAP: int = _int("CHUNK_WINDOW_OVERLAP", 800)
    # Deterministic fallback splitter (used if the LLM call fails).
    FALLBACK_CHUNK_SIZE: int = _int("FALLBACK_CHUNK_SIZE", 1100)
    FALLBACK_CHUNK_OVERLAP: int = _int("FALLBACK_CHUNK_OVERLAP", 150)

    # --- Document reading (LLM reads the file directly) -------------------- #
    # native = the PDF pages are sent straight to CHUNK_MODEL (best fidelity,
    #          OCRs scans, reads tables/Arabic); text = PyMuPDF extraction first.
    PDF_INGEST_MODE: str = _str("PDF_INGEST_MODE", "native")        # native | text
    # OpenRouter file-parser engine for PDFs:
    #   native       -> pages handed to the model as-is (Gemini); charged as input tokens
    #   mistral-ocr  -> dedicated OCR, best for heavily scanned/handwritten docs
    #   cloudflare-ai-> PDF converted to markdown (free)
    PDF_ENGINE: str = _str("PDF_ENGINE", "native")
    # PDFs are sent in page batches so big files never truncate and we keep pages.
    PDF_PAGES_PER_BATCH: int = _int("PDF_PAGES_PER_BATCH", 6)
    # Max output tokens for a read/chunk call (full transcription can be large).
    READ_MAX_TOKENS: int = _int("READ_MAX_TOKENS", 16000)
    # Target chunk size guidance given to the model (words).
    CHUNK_TARGET_WORDS: int = _int("CHUNK_TARGET_WORDS", 250)
    # Max PDF upload size to send to the reader (MB) before requiring text mode.
    PDF_MAX_MB: int = _int("PDF_MAX_MB", 50)

    # --- Q&A generation (documents -> bilingual Q&A knowledge base) -------- #
    # qa     = the model reads the file and writes Q&A pairs (variants, AR+EN)
    # chunks = classic semantic chunking (kept as an option)
    INGEST_STRATEGY: str = _str("INGEST_STRATEGY", "qa")
    QA_VARIANTS: int = _int("QA_VARIANTS", 4)          # question paraphrases per language
    # One page per call: dense tables (hundreds of rows) need the whole output
    # budget for a single page or coverage silently truncates.
    QA_PAGES_PER_BATCH: int = _int("QA_PAGES_PER_BATCH", 1)
    QA_MAX_TOKENS: int = _int("QA_MAX_TOKENS", 32000)

    # --- Vector store & retrieval ------------------------------------------ #
    COLLECTION_NAME: str = _str("COLLECTION_NAME", "ju_knowledge")
    MEMORY_COLLECTION: str = _str("MEMORY_COLLECTION", "ju_memory")
    # Q&A records are short, so a deeper top-k is cheap and helps multi-fact
    # questions (e.g. "how are the 161 hours distributed?").
    TOP_K: int = _int("TOP_K", 10)
    # Over-fetch pool: recency re-rank + paraphrase dedupe both need headroom.
    CANDIDATE_K: int = _int("CANDIDATE_K", 48)
    SCORE_THRESHOLD: float = _float("SCORE_THRESHOLD", 0.15)  # min cosine similarity

    # Recency: blended score = (1-w)*similarity + w*recency, recency decays by half
    # every RECENCY_HALFLIFE_DAYS. Newest data therefore wins on near-ties.
    RECENCY_WEIGHT: float = _float("RECENCY_WEIGHT", 0.18)
    RECENCY_HALFLIFE_DAYS: float = _float("RECENCY_HALFLIFE_DAYS", 365.0)

    # --- Memory cache (admin-promoted Q&A, served without spending tokens) -- #
    MEMORY_SIM_THRESHOLD: float = _float("MEMORY_SIM_THRESHOLD", 0.92)

    # --- Web search agent (searches the official JU website live) ----------- #
    # The student can opt in PER MESSAGE (a toggle in the chat composer); this
    # flag only controls whether that toggle is AVAILABLE to students at all.
    WEB_SEARCH_ENABLED: bool = _bool("WEB_SEARCH_ENABLED", True)
    WEB_SEARCH_DOMAIN: str = _str("WEB_SEARCH_DOMAIN", "ju.edu.jo")
    # Provider: auto picks Serper (Google) when SERPER_API_KEY is set, else
    # Tavily, else the OpenRouter web plugin. Force one with:
    #   serper | tavily | openrouter | duckduckgo.
    WEB_SEARCH_PROVIDER: str = _str("WEB_SEARCH_PROVIDER", "auto")
    TAVILY_API_KEY: str = _str("TAVILY_API_KEY")
    TAVILY_SEARCH_DEPTH: str = _str("TAVILY_SEARCH_DEPTH", "advanced")  # basic | advanced
    # Tavily synthesizes a direct answer from the JU pages it finds — a big
    # quality boost (measured: real, correct Arabic answers). Keep this on.
    WEB_SEARCH_INCLUDE_ANSWER: bool = _bool("WEB_SEARCH_INCLUDE_ANSWER", True)
    TAVILY_CHUNKS_PER_SOURCE: int = _int("TAVILY_CHUNKS_PER_SOURCE", 3)
    # Serper.dev = Google Search via API — the best ranking/coverage for a known
    # site like ju.edu.jo. Get a key at https://serper.dev (free tier, then cheap).
    SERPER_API_KEY: str = _str("SERPER_API_KEY")
    # JU's site is Arabic-first; bias Serper's locale to Jordan/Arabic.
    SERPER_GL: str = _str("SERPER_GL", "jo")   # geo
    SERPER_HL: str = _str("SERPER_HL", "ar")   # language
    WEB_SEARCH_RESULTS: int = _int("WEB_SEARCH_RESULTS", 6)       # pages per search
    # Auto-fallback: when the student did NOT toggle web search on but the local
    # knowledge base clearly doesn't cover the question (best match below the
    # threshold), search the JU site automatically. Makes the bot answer about
    # anything on ju.edu.jo without the student needing to flip the toggle.
    WEB_SEARCH_AUTO: bool = _bool("WEB_SEARCH_AUTO", True)
    WEB_SEARCH_PAGE_CHARS: int = _int("WEB_SEARCH_PAGE_CHARS", 3500)  # chars kept per page
    WEB_SEARCH_RESULT_CHARS: int = _int("WEB_SEARCH_RESULT_CHARS", 14000)  # cap per tool result
    WEB_SEARCH_MAX_ITERS: int = _int("WEB_SEARCH_MAX_ITERS", 3)   # tool-call rounds per turn
    WEB_SEARCH_TIMEOUT_S: int = _int("WEB_SEARCH_TIMEOUT_S", 15)

    # --- Conversation / limits --------------------------------------------- #
    CHAT_LIMIT_PER_SESSION: int = _int("CHAT_LIMIT_PER_SESSION", 30)
    CHAT_HISTORY_TURNS: int = _int("CHAT_HISTORY_TURNS", 4)

    # --- Deployment / security --------------------------------------------- #
    # Flask debug (Werkzeug debugger = RCE if exposed). MUST stay off in prod.
    DEBUG: bool = _bool("DEBUG", False)
    # Send session cookies only over HTTPS (enable in production behind TLS).
    COOKIE_SECURE: bool = _bool("COOKIE_SECURE", False)
    # Trust X-Forwarded-* headers (enable ONLY behind a reverse proxy you own).
    TRUST_PROXY: bool = _bool("TRUST_PROXY", False)

    # --- Abuse protection (public chat) ------------------------------------ #
    # Per-IP request ceilings. 0 disables that window.
    RATE_LIMIT_PER_MIN: int = _int("RATE_LIMIT_PER_MIN", 12)
    RATE_LIMIT_PER_HOUR: int = _int("RATE_LIMIT_PER_HOUR", 120)
    RATE_LIMIT_PER_DAY: int = _int("RATE_LIMIT_PER_DAY", 600)
    # Monthly spend ceiling (USD). 0 = unlimited. Also editable in the Cost page.
    MONTHLY_BUDGET_USD: float = _float("MONTHLY_BUDGET_USD", 0.0)

    # --- Web & auth -------------------------------------------------------- #
    SECRET_KEY: str = _str("SECRET_KEY", "dev-secret-change-me")
    ADMIN_USERNAME: str = _str("ADMIN_USERNAME", "admin")
    ADMIN_PASSWORD: str = _str("ADMIN_PASSWORD", "admin123")  # seeded once; change it
    # Admin must re-login when the browser closes; also auto-logout after this many
    # idle minutes. Lower it for stricter re-auth (0 = only browser-close logout).
    ADMIN_SESSION_MINUTES: int = _int("ADMIN_SESSION_MINUTES", 30)
    MAX_UPLOAD_MB: int = _int("MAX_UPLOAD_MB", 40)
    ALLOWED_EXTENSIONS: set[str] = {"pdf", "docx", "txt"}

    # --------------------------------------------------------------------- #
    @property
    def max_content_length(self) -> int:
        return self.MAX_UPLOAD_MB * 1024 * 1024

    def ensure_dirs(self) -> None:
        """Create all runtime directories (idempotent)."""
        for d in (
            self.DATA_DIR,
            self.UPLOAD_DIR,
            self.QA_DIR,
            self.PROCESSED_DIR,
            self.CHROMA_DIR,
        ):
            d.mkdir(parents=True, exist_ok=True)

    def is_configured(self) -> bool:
        """True if the OpenRouter key is present (the only hard requirement)."""
        return bool(self.OPENROUTER_API_KEY)


settings = Config()
settings.ensure_dirs()

"""Token usage accounting and cost estimation.

Every OpenRouter call (chat, query condensation, embeddings, document reading,
Q&A generation) reports a ``usage`` object with real token counts. We record
those into the ``token_usage`` table, attributed — via a context variable — to
the document being ingested or the chat session that triggered them. The admin
Cost page then aggregates tokens and multiplies by the (editable) per-model
price to show approximate spend.

Costs are computed from stored TOKENS × CURRENT price at display time, so
editing the price table instantly re-prices all history (no stale snapshots).
"""

from __future__ import annotations

import contextvars
import json
import logging
import re
from typing import Optional

from ..config import settings
from . import db

log = logging.getLogger(__name__)

# Per-thread attribution for whatever low-level call happens next.
_ctx: contextvars.ContextVar = contextvars.ContextVar("usage_ctx", default=None)

# Human labels (Arabic) for the operations we meter.
OPERATION_LABELS = {
    "speech": "قراءة الإجابات صوتياً",
    "speech_diacritics": "تشكيل النص للقراءة الصوتية",
    "chat": "إجابات المحادثة",
    "condense": "إعادة صياغة السؤال",
    "embed": "التضمين (Embeddings)",
    "read": "قراءة المستندات",
    "qa": "توليد الأسئلة والأجوبة",
    "chunk": "التقطيع الدلالي",
    "web_search": "البحث في موقع الجامعة",
}


# --------------------------------------------------------------------------- #
#  Attribution context (propagated into worker threads)                       #
# --------------------------------------------------------------------------- #
def set_context(*, doc_id: Optional[int] = None, session_token: Optional[str] = None):
    return _ctx.set({"doc_id": doc_id, "session_token": session_token})


def reset_context(token) -> None:
    try:
        _ctx.reset(token)
    except (ValueError, LookupError):
        pass


def with_context(fn):
    """Wrap ``fn`` so it runs in worker threads with the CURRENT context.

    ``ThreadPoolExecutor`` does not copy context vars into its workers; we
    capture the value here (in the submitting thread) and re-bind it inside
    each worker so usage stays attributed to the right document/session.
    """
    val = _ctx.get()

    def wrapped(*args, **kwargs):
        tok = _ctx.set(val)
        try:
            return fn(*args, **kwargs)
        finally:
            reset_context(tok)

    return wrapped


# --------------------------------------------------------------------------- #
#  Recording                                                                  #
# --------------------------------------------------------------------------- #
def _split_usage(usage) -> tuple[int, int]:
    if usage is None:
        return 0, 0
    p = getattr(usage, "prompt_tokens", None)
    c = getattr(usage, "completion_tokens", None)
    if p is None and isinstance(usage, dict):
        p = usage.get("prompt_tokens")
        c = usage.get("completion_tokens")
    return int(p or 0), int(c or 0)


def usage_cost(usage) -> Optional[float]:
    """Extract OpenRouter's ACTUAL charged cost (USD) from a usage object.

    Returned only when the call requested ``usage: {include: true}``; otherwise
    ``None`` so the caller falls back to the price table.
    """
    if usage is None:
        return None
    val = getattr(usage, "cost", None)
    if val is None:
        extra = getattr(usage, "model_extra", None)
        if isinstance(extra, dict):
            val = extra.get("cost")
    if val is None and isinstance(usage, dict):
        val = usage.get("cost")
    try:
        return float(val) if val is not None else None
    except (TypeError, ValueError):
        return None


def record(model: str, operation: str, usage=None, *,
           prompt_tokens: Optional[int] = None,
           completion_tokens: Optional[int] = None,
           cost_usd: Optional[float] = None, estimated: bool = False) -> None:
    """Persist one usage event. Never raises — accounting must not break work.

    ``cost_usd`` — pass OpenRouter's real cost when available; if omitted it is
    derived from the editable price table (used for embeddings + backfill).
    """
    try:
        if usage is not None:
            p, c = _split_usage(usage)
            if cost_usd is None:
                cost_usd = usage_cost(usage)
        else:
            p, c = int(prompt_tokens or 0), int(completion_tokens or 0)
        if p == 0 and c == 0:
            return
        if cost_usd is None:
            cost_usd = cost(model, p, c)
        ctx = _ctx.get() or {}
        db.add_token_usage(
            model=model, operation=operation, prompt_tokens=p, completion_tokens=c,
            cost_usd=float(cost_usd or 0.0), estimated=1 if estimated else 0,
            doc_id=ctx.get("doc_id"), session_token=ctx.get("session_token"),
        )
    except Exception:  # noqa: BLE001
        log.debug("token usage record failed", exc_info=True)


# --------------------------------------------------------------------------- #
#  Pricing (config defaults + admin overrides stored in settings)             #
# --------------------------------------------------------------------------- #
def get_pricing() -> dict:
    """Effective price table: config defaults merged with admin overrides."""
    pricing = {m: dict(v) for m, v in settings.DEFAULT_PRICING.items()}
    raw = db.get_setting("token_pricing")
    if raw:
        try:
            for m, v in (json.loads(raw) or {}).items():
                pricing[m] = {
                    "input": float(v.get("input", 0) or 0),
                    "output": float(v.get("output", 0) or 0),
                }
        except (json.JSONDecodeError, TypeError, ValueError):
            log.warning("Stored token_pricing is invalid; using defaults.")
    return pricing


def save_pricing(pricing: dict) -> None:
    clean = {
        str(m): {"input": float(v.get("input", 0) or 0),
                 "output": float(v.get("output", 0) or 0)}
        for m, v in pricing.items() if str(m).strip()
    }
    db.set_setting("token_pricing", json.dumps(clean, ensure_ascii=False))


def fetch_openrouter_prices(models: list[str]) -> dict:
    """Fetch live per-1M prices for ``models`` from OpenRouter's /models API.

    OpenRouter lists chat models only (no embedding models), so the embedding
    model simply won't be returned — its rate is kept from the existing table.
    Raises on network/HTTP errors so the caller can report failure.
    """
    import json
    import urllib.request

    url = settings.OPENROUTER_BASE_URL.rstrip("/") + "/models"
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {settings.OPENROUTER_API_KEY}"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.load(resp).get("data", [])
    wanted = set(models)
    out: dict = {}
    for m in data:
        if m.get("id") in wanted:
            p = m.get("pricing", {}) or {}
            try:
                out[m["id"]] = {
                    "input": round(float(p.get("prompt", 0)) * 1_000_000, 6),
                    "output": round(float(p.get("completion", 0)) * 1_000_000, 6),
                }
            except (TypeError, ValueError):
                continue
    return out


def refresh_pricing_from_openrouter() -> dict:
    """Update the stored price table with live OpenRouter rates. Returns it."""
    pricing = get_pricing()
    live = fetch_openrouter_prices(list(pricing.keys()))
    pricing.update(live)
    save_pricing(pricing)
    return live


def price_for(model: str, pricing: Optional[dict] = None) -> dict:
    pricing = pricing if pricing is not None else get_pricing()
    return pricing.get(model, {"input": 0.0, "output": 0.0})


def cost(model: str, prompt_tokens: int, completion_tokens: int,
         pricing: Optional[dict] = None) -> float:
    pr = price_for(model, pricing)
    return ((prompt_tokens / 1_000_000.0) * pr["input"]
            + (completion_tokens / 1_000_000.0) * pr["output"])


# --------------------------------------------------------------------------- #
#  Token estimation (for the interactive calculator only)                     #
# --------------------------------------------------------------------------- #
_ARABIC = re.compile(r"[؀-ۿݐ-ݿࢠ-ࣿ]")


def estimate_tokens(text: str) -> int:
    """Rough bilingual estimate: Arabic ~2 chars/token, Latin ~4 chars/token."""
    if not text:
        return 0
    arabic = len(_ARABIC.findall(text))
    other = len(text) - arabic
    return int(round(arabic / 2.0 + other / 4.0))

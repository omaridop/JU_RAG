"""Single OpenRouter (OpenAI-compatible) client, shared by every layer.

OpenRouter serves both the chat model and the embeddings model behind one API
key, so one client is all we need.

OpenRouter quirk handled here: upstream provider failures are sometimes
returned as HTTP 200 with an ``error`` payload in the body (and ``choices`` /
``data`` set to null). :class:`ProviderResponseError` represents that case so
callers can retry it like any other transient API error instead of crashing
with ``'NoneType' object is not iterable``.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any, Optional

from openai import OpenAI

from ..config import settings


class LLMNotConfigured(RuntimeError):
    """Raised when the OpenRouter key is missing."""


class ProviderResponseError(RuntimeError):
    """The provider returned HTTP 200 but an error/empty payload in the body."""


def response_error(resp: Any) -> Optional[str]:
    """Extract the error message OpenRouter embeds in a 200-OK body, if any."""
    err = getattr(resp, "error", None)
    if not err:
        return None
    if isinstance(err, dict):
        return str(err.get("message") or err)
    return str(err)


def first_choice_content(resp: Any) -> str:
    """Return ``choices[0].message.content`` or raise ProviderResponseError."""
    choices = getattr(resp, "choices", None)
    if not choices:
        raise ProviderResponseError(
            response_error(resp) or "Provider returned a response with no choices."
        )
    return choices[0].message.content or ""


@lru_cache(maxsize=1)
def get_client() -> OpenAI:
    if not settings.OPENROUTER_API_KEY:
        raise LLMNotConfigured(
            "OPENROUTER_API_KEY is not set. Copy .env.example to .env and add your key."
        )
    return OpenAI(
        api_key=settings.OPENROUTER_API_KEY,
        base_url=settings.OPENROUTER_BASE_URL,
        # OpenRouter uses these for its public app rankings; harmless otherwise.
        default_headers={
            "HTTP-Referer": settings.OPENROUTER_REFERER,
            "X-Title": settings.OPENROUTER_TITLE,
        },
        timeout=float(settings.LLM_TIMEOUT_S),
        max_retries=0,  # retries handled explicitly (tenacity) for finer control
    )

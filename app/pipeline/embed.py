"""Embeddings via OpenRouter -> Gemini Embedding 2.

All vectors are L2-normalized so the Chroma collection (cosine space) behaves
consistently regardless of whether the provider returns normalized output.

Robustness:
  * OpenRouter occasionally wraps upstream failures in an HTTP 200 whose body
    has ``data: null`` — that is detected and retried (it used to crash whole
    ingests with ``'NoneType' object is not iterable``).
  * Inputs are truncated to ``EMBED_MAX_CHARS`` so an over-long string can
    never blow a provider's per-input token limit.
  * A batch that keeps failing is bisected so one poisoned input cannot sink
    the whole document; the offending input is identified in the logs.
  * Batches are embedded concurrently (``EMBED_CONCURRENCY``) with order
    preserved.
"""

from __future__ import annotations

import logging
import math
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional, Sequence

from openai import APIError, APITimeoutError, RateLimitError
from tenacity import (retry, retry_if_exception_type, stop_after_attempt,
                      wait_exponential)

from ..config import settings
from ..core import tokens
from ..core.llm import ProviderResponseError, get_client, response_error

log = logging.getLogger(__name__)

_RETRYABLE = (RateLimitError, APITimeoutError, APIError, ProviderResponseError)

ProgressCb = Optional[Callable[[int, int], None]]  # (done, total)


def _normalize(vec: Sequence[float]) -> list[float]:
    norm = math.sqrt(sum(float(x) * float(x) for x in vec))
    if norm == 0.0:
        return [float(x) for x in vec]
    return [float(x) / norm for x in vec]


@retry(
    retry=retry_if_exception_type(_RETRYABLE),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    stop=stop_after_attempt(5),
    reraise=True,
)
def _embed_raw(inputs: list[str], *, send_dimensions: bool) -> list[list[float]]:
    client = get_client()
    kwargs: dict = {
        "model": settings.EMBED_MODEL,
        "input": inputs,
        "encoding_format": "float",
    }
    if send_dimensions and settings.EMBED_DIM > 0:
        kwargs["dimensions"] = settings.EMBED_DIM
    resp = client.embeddings.create(timeout=float(settings.EMBED_TIMEOUT_S), **kwargs)

    data = getattr(resp, "data", None)
    if not data:
        # OpenRouter wraps upstream failures in a 200 with an error body.
        raise ProviderResponseError(
            response_error(resp) or "Embedding provider returned no data.")
    if len(data) != len(inputs):
        raise ProviderResponseError(
            f"Embedding count mismatch: sent {len(inputs)} inputs, got {len(data)}.")

    tokens.record(settings.EMBED_MODEL, "embed", getattr(resp, "usage", None))

    # Provider order is not guaranteed; sort by the returned index.
    items = sorted(data, key=lambda d: getattr(d, "index", 0) or 0)
    vectors: list[list[float]] = []
    for d in items:
        emb = getattr(d, "embedding", None)
        if not emb:
            raise ProviderResponseError("Embedding provider returned an empty vector.")
        vectors.append(list(emb))
    return vectors


def _embed_batch(inputs: list[str]) -> list[list[float]]:
    """Embed one batch, gracefully retrying without `dimensions` if rejected."""
    try:
        return _embed_raw(inputs, send_dimensions=True)
    except APIError as exc:
        # Some providers don't accept the `dimensions` param; retry without it.
        if settings.EMBED_DIM > 0 and _is_bad_request(exc):
            log.warning("Embedding provider rejected `dimensions`; retrying without it.")
            return _embed_raw(inputs, send_dimensions=False)
        raise


def _is_bad_request(exc: APIError) -> bool:
    status = getattr(exc, "status_code", None)
    return status == 400 or "dimension" in str(exc).lower()


def _embed_resilient(inputs: list[str]) -> list[list[float]]:
    """Embed a batch; if it keeps failing, bisect it to isolate the bad input."""
    try:
        return _embed_batch(inputs)
    except Exception as exc:  # noqa: BLE001 - we re-raise after narrowing
        if len(inputs) == 1:
            log.error("Embedding failed for input starting with %r: %s",
                      inputs[0][:80], exc)
            raise
        mid = len(inputs) // 2
        log.warning("Embedding batch of %d failed (%s); splitting to isolate.",
                    len(inputs), exc)
        return _embed_resilient(inputs[:mid]) + _embed_resilient(inputs[mid:])


def embed_texts(texts: Sequence[str], on_progress: ProgressCb = None) -> list[list[float]]:
    """Embed many texts (auto-batched, concurrent), normalized, in order."""
    limit = max(500, settings.EMBED_MAX_CHARS)
    cleaned = [((t or " ").strip() or " ")[:limit] for t in texts]
    if not cleaned:
        return []

    batch = max(1, settings.EMBED_BATCH)
    batches = [cleaned[i:i + batch] for i in range(0, len(cleaned), batch)]
    workers = max(1, min(settings.EMBED_CONCURRENCY, len(batches)))

    out: list[list[float]] = []
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        # ex.map preserves batch order, so vectors stay aligned with inputs.
        # with_context re-binds the usage attribution inside each worker thread.
        for bi, vectors in enumerate(ex.map(tokens.with_context(_embed_resilient), batches)):
            out.extend(_normalize(v) for v in vectors)
            done += len(batches[bi])
            if on_progress:
                try:
                    on_progress(done, len(cleaned))
                except Exception:  # noqa: BLE001 - progress must never break embedding
                    pass
            log.debug("Embedded %d/%d texts", done, len(cleaned))
    return out


def embed_query(text: str) -> list[float]:
    """Embed a single query string -> one normalized vector."""
    return embed_texts([text])[0]


def embedding_dim() -> int:
    """Probe the live embedding dimensionality (used for diagnostics)."""
    return len(embed_query("dimension probe"))

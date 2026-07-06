"""Semantic chunking with DeepSeek V4 Flash.

The document is split into coarse windows (whole pages where possible), and the
LLM is asked to break each window into self-contained, *verbatim* chunks with a
title, one-line summary, keywords, and language. If the model call fails for any
reason, a deterministic paragraph splitter takes over so ingestion never aborts.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Optional

from openai import APIError
from tenacity import (retry, retry_if_exception_type, stop_after_attempt,
                      wait_exponential)

from ..config import settings
from ..core import tokens
from ..core.llm import ProviderResponseError, first_choice_content, get_client
from ..core.utils import detect_lang
from .extract import ExtractedDoc

log = logging.getLogger(__name__)

CHUNKER_SYSTEM_PROMPT = """\
You are an expert document analyst preparing university documents (Arabic and \
English) for a retrieval system. You will receive one section of a document.

Split it into semantically coherent, self-contained chunks. Rules:
1. Each chunk must stand on its own and cover ONE topic or idea.
2. Copy the chunk text VERBATIM from the source. Do NOT translate, paraphrase, \
summarize, reorder, or fix wording inside "text". Preserve every number, date, \
name, fee, and policy exactly as written.
3. Prefer chunks of roughly 80-300 characters, but always respect natural topic \
boundaries (headings, list groups, Q/A units) over hitting a size.
4. Drop pure noise (page numbers, repeated headers/footers, navigation).
5. Detect the language of each chunk: "ar", "en", or "mixed".

Return STRICT JSON only, no markdown, in exactly this shape:
{"chunks": [
  {"title": "...", "summary": "one concise sentence", "keywords": ["...","..."], \
"language": "ar|en|mixed", "text": "verbatim chunk text"}
]}
If the section contains no useful content, return {"chunks": []}."""


@dataclass
class Chunk:
    text: str
    title: str
    summary: str
    keywords: list[str]
    lang: str
    page_start: Optional[int]
    page_end: Optional[int]


# --------------------------------------------------------------------------- #
#  Windowing                                                                  #
# --------------------------------------------------------------------------- #
def _split_by_size(text: str, size: int, overlap: int) -> list[str]:
    """Split oversized text on paragraph/sentence boundaries with overlap."""
    paras = [p for p in re.split(r"\n{2,}", text) if p.strip()]
    pieces, buf = [], ""
    for p in paras:
        if len(p) > size:  # a single huge paragraph -> hard slice
            if buf:
                pieces.append(buf)
                buf = ""
            for i in range(0, len(p), size - overlap):
                pieces.append(p[i:i + size])
            continue
        if buf and len(buf) + len(p) + 2 > size:
            pieces.append(buf)
            buf = buf[-overlap:] if overlap else ""
            buf = (buf + "\n\n" + p) if buf else p
        else:
            buf = (buf + "\n\n" + p) if buf else p
    if buf.strip():
        pieces.append(buf)
    return pieces


def _build_windows(doc: ExtractedDoc) -> list[dict]:
    """Group pages into ~CHUNK_WINDOW_CHARS windows, preserving page numbers."""
    win_chars = settings.CHUNK_WINDOW_CHARS
    overlap = settings.CHUNK_WINDOW_OVERLAP
    windows: list[dict] = []
    buf, pstart, pend = "", None, None

    def flush():
        nonlocal buf, pstart, pend
        if buf.strip():
            windows.append({"text": buf.strip(), "page_start": pstart, "page_end": pend})
        buf, pstart, pend = "", None, None

    for page in doc.pages:
        text = page.text
        if len(text) > win_chars:
            flush()
            for piece in _split_by_size(text, win_chars, overlap):
                windows.append({"text": piece, "page_start": page.page, "page_end": page.page})
            continue
        if buf and len(buf) + len(text) + 2 > win_chars:
            flush()
        if not buf:
            pstart = page.page
        buf = (buf + "\n\n" + text) if buf else text
        pend = page.page
    flush()
    return windows


# --------------------------------------------------------------------------- #
#  LLM chunking                                                               #
# --------------------------------------------------------------------------- #
def _parse_json(content: str) -> dict:
    s = content.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\n?", "", s)
        s = re.sub(r"\n?```$", "", s).strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        start, end = s.find("{"), s.rfind("}")
        if 0 <= start < end:
            return json.loads(s[start:end + 1])
        raise


@retry(
    retry=retry_if_exception_type((APIError, ProviderResponseError)),
    wait=wait_exponential(multiplier=1, min=2, max=20),
    stop=stop_after_attempt(3),
    reraise=True,
)
def _call_llm(text: str, *, use_json_mode: bool) -> str:
    client = get_client()
    kwargs: dict = {
        "model": settings.CHAT_MODEL,
        "messages": [
            {"role": "system", "content": CHUNKER_SYSTEM_PROMPT},
            {"role": "user", "content": f"Document section:\n\n{text}"},
        ],
        "temperature": settings.CHUNK_TEMPERATURE,
        "max_tokens": settings.CHUNK_MAX_TOKENS,
    }
    if use_json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    kwargs["extra_body"] = {"usage": {"include": True}}  # ask for the real charged cost
    resp = client.chat.completions.create(
        timeout=float(settings.INGEST_TIMEOUT_S), **kwargs)
    content = first_choice_content(resp)
    usage = getattr(resp, "usage", None)
    tokens.record(settings.CHAT_MODEL, "chunk", usage, cost_usd=tokens.usage_cost(usage))
    return content


def _llm_chunk_window(text: str) -> list[dict]:
    try:
        content = _call_llm(text, use_json_mode=True)
    except APIError as exc:
        # Model/route may not support JSON mode -> retry relying on the prompt.
        if getattr(exc, "status_code", None) in (400, 404, 422):
            log.warning("JSON mode unsupported; retrying chunker without response_format.")
            content = _call_llm(text, use_json_mode=False)
        else:
            raise
    data = _parse_json(content)
    chunks = data.get("chunks", []) if isinstance(data, dict) else data
    return chunks if isinstance(chunks, list) else []


def _normalize_chunk(raw: dict, win: dict) -> Optional[Chunk]:
    text = (raw.get("text") or "").strip()
    if not text:
        return None
    kw = raw.get("keywords") or []
    if isinstance(kw, str):
        kw = [k.strip() for k in kw.split(",") if k.strip()]
    lang = raw.get("language") or detect_lang(text)
    return Chunk(
        text=text,
        title=(raw.get("title") or "").strip(),
        summary=(raw.get("summary") or "").strip(),
        keywords=[str(k).strip() for k in kw][:10],
        lang=lang if lang in ("ar", "en", "mixed") else detect_lang(text),
        page_start=win.get("page_start"),
        page_end=win.get("page_end"),
    )


# --------------------------------------------------------------------------- #
#  Fallback splitter                                                          #
# --------------------------------------------------------------------------- #
def _fallback_chunks(win: dict) -> list[Chunk]:
    pieces = _split_by_size(win["text"], settings.FALLBACK_CHUNK_SIZE,
                            settings.FALLBACK_CHUNK_OVERLAP)
    out = []
    for p in pieces:
        p = p.strip()
        if p:
            out.append(Chunk(text=p, title="", summary="", keywords=[],
                             lang=detect_lang(p),
                             page_start=win.get("page_start"),
                             page_end=win.get("page_end")))
    return out


# --------------------------------------------------------------------------- #
#  Public API                                                                 #
# --------------------------------------------------------------------------- #
def semantic_chunks(doc: ExtractedDoc) -> list[Chunk]:
    """Chunk an extracted document. LLM-first, deterministic fallback on error."""
    windows = _build_windows(doc)
    log.info("Chunking %s: %d window(s)", doc.filename, len(windows))
    chunks: list[Chunk] = []
    for idx, win in enumerate(windows, start=1):
        try:
            raw_chunks = _llm_chunk_window(win["text"])
            window_chunks = [c for c in (_normalize_chunk(r, win) for r in raw_chunks) if c]
            if not window_chunks:  # model returned nothing usable
                window_chunks = _fallback_chunks(win)
        except Exception as exc:  # noqa: BLE001 - never abort ingestion
            log.warning("LLM chunking failed for window %d (%s); using fallback.", idx, exc)
            window_chunks = _fallback_chunks(win)
        chunks.extend(window_chunks)
    log.info("Chunking %s produced %d chunk(s)", doc.filename, len(chunks))
    return chunks

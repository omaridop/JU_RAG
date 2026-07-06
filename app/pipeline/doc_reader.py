"""LLM document reader: the model reads the file directly and returns chunks.

PDFs are sent **natively** to ``CHUNK_MODEL`` (e.g. Gemini 2.5 Flash) via
OpenRouter's file input + file-parser plugin, so the model truly *reads* the
pages — OCR, layout, tables, Arabic — instead of relying on a text extractor.
Big PDFs are sent in page batches so nothing is truncated and page numbers are
preserved. DOCX files are read from their (reliable) XML text. Every path has a
deterministic fallback so ingestion never aborts.
"""

from __future__ import annotations

import base64
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Optional

import fitz  # PyMuPDF (used to count/split pages and as a text fallback)
from openai import APIError
from tenacity import (retry, retry_if_exception_type, stop_after_attempt,
                      wait_exponential)

from ..config import settings
from ..core import tokens
from ..core.llm import ProviderResponseError, first_choice_content, get_client
from ..core.utils import detect_lang
from .chunker import Chunk, _parse_json, _split_by_size
from .extract import extract_docx, extract_txt

log = logging.getLogger(__name__)

ProgressCb = Optional[Callable[[str, str], None]]


def _reader_prompt() -> str:
    n = settings.CHUNK_TARGET_WORDS
    return (
        "You are reading a University of Jordan document (Arabic, English, or "
        "mixed; it may be scanned). Read and transcribe ALL of its content in "
        "complete detail — every page, paragraph, heading, list item, table, "
        "footnote, caption and number. Do NOT summarize, translate, reorder, or "
        "skip anything. Transcribe Arabic exactly and preserve every number, "
        "date, name, fee and regulation. Render tables as text rows with cells "
        "separated by ' | '.\n\n"
        f"Then split the content into semantically coherent, self-contained "
        f"chunks of roughly {n} words each, but respect natural boundaries "
        "(headings, list groups, Q/A units, whole tables).\n\n"
        'Return STRICT JSON only, no markdown:\n'
        '{"chunks":[{"title":"...","summary":"one sentence",'
        '"keywords":["..."],"language":"ar|en|mixed","page":<page number>,'
        '"text":"verbatim transcribed text"}]}\n'
        "Use the page number shown on each page. Cover EVERY page provided. "
        'If there is no usable content, return {"chunks":[]}.'
    )


TEXT_CHUNK_SYSTEM = (
    "You are an expert analyst preparing university documents (Arabic/English) "
    "for retrieval. Split the provided text into semantically coherent, "
    "self-contained chunks of roughly {n} words each, respecting natural "
    "boundaries (headings, lists, Q/A units, tables). Copy the chunk text "
    "VERBATIM — do not translate, paraphrase, summarize, or alter numbers, "
    "dates, names, fees or policies. Detect each chunk's language.\n"
    'Return STRICT JSON only: {"chunks":[{"title":"...","summary":"one '
    'sentence","keywords":["..."],"language":"ar|en|mixed","text":"verbatim '
    'text"}]}. If there is no useful content, return {"chunks":[]}.'
)


# --------------------------------------------------------------------------- #
#  Low-level model calls                                                       #
# --------------------------------------------------------------------------- #
@retry(retry=retry_if_exception_type((APIError, ProviderResponseError)),
       wait=wait_exponential(multiplier=1, min=2, max=20),
       stop=stop_after_attempt(3), reraise=True)
def _request(messages: list[dict], *, json_mode: bool, plugins: Optional[list]) -> str:
    kwargs: dict = {
        "model": settings.CHUNK_MODEL,
        "messages": messages,
        "temperature": settings.CHUNK_TEMPERATURE,
        "max_tokens": settings.READ_MAX_TOKENS,
    }
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    extra_body: dict = {"usage": {"include": True}}  # ask for the real charged cost
    if plugins:
        extra_body["plugins"] = plugins
    kwargs["extra_body"] = extra_body
    # Full-page transcription can take minutes -> long per-request timeout.
    resp = get_client().chat.completions.create(
        timeout=float(settings.INGEST_TIMEOUT_S), **kwargs)
    content = first_choice_content(resp)
    usage = getattr(resp, "usage", None)
    tokens.record(settings.CHUNK_MODEL, "read", usage, cost_usd=tokens.usage_cost(usage))
    return content


def _call(messages: list[dict], *, plugins: Optional[list] = None) -> list[dict]:
    """Call the model, tolerating providers that reject JSON mode."""
    try:
        content = _request(messages, json_mode=True, plugins=plugins)
    except APIError as exc:
        if getattr(exc, "status_code", None) in (400, 404, 422):
            log.warning("JSON mode unsupported; retrying reader without response_format.")
            content = _request(messages, json_mode=False, plugins=plugins)
        else:
            raise
    data = _parse_json(content)
    chunks = data.get("chunks", []) if isinstance(data, dict) else data
    return chunks if isinstance(chunks, list) else []


def _normalize(raw: dict, *, page_start: Optional[int], page_end: Optional[int]) -> Optional[Chunk]:
    text = (raw.get("text") or "").strip()
    if not text:
        return None
    kw = raw.get("keywords") or []
    if isinstance(kw, str):
        kw = [k.strip() for k in kw.split(",") if k.strip()]
    lang = raw.get("language") or detect_lang(text)
    # The model may report the position inside the page batch (1..span) or the
    # printed absolute page number — accept both, drop anything implausible.
    pg = raw.get("page")
    try:
        pg = int(pg)
    except (TypeError, ValueError):
        pg = None
    if pg is not None and page_start and page_end:
        span = page_end - page_start + 1
        if page_start <= pg <= page_end:
            pass                          # already absolute
        elif 1 <= pg <= span:
            pg = page_start + pg - 1      # batch-local -> absolute
        else:
            pg = None
    return Chunk(
        text=text,
        title=(raw.get("title") or "").strip(),
        summary=(raw.get("summary") or "").strip(),
        keywords=[str(k).strip() for k in kw][:10],
        lang=lang if lang in ("ar", "en", "mixed") else detect_lang(text),
        page_start=pg or page_start,
        page_end=pg or page_end,
    )


# --------------------------------------------------------------------------- #
#  PDF — native reading (page-batched)                                         #
# --------------------------------------------------------------------------- #
def _pdf_batch_bytes(path: Path, start: int, end: int) -> bytes:
    """Return a new PDF containing pages [start, end) (0-based) as bytes."""
    with fitz.open(str(path)) as src:
        out = fitz.open()
        out.insert_pdf(src, from_page=start, to_page=end - 1)
        data = out.tobytes()
        out.close()
    return data


def _pages_text(path: Path, start: int, end: int) -> str:
    with fitz.open(str(path)) as src:
        return "\n\n".join(src[i].get_text("text") for i in range(start, end))


def _read_batch(path: Path, start: int, end: int, total: int,
                plugins: list, on_progress: ProgressCb) -> list[Chunk]:
    """Read+chunk PDF pages [start, end) — one worker task."""
    if on_progress:
        on_progress("reading", f"pages {start + 1}-{end} of {total}")
    try:
        b64 = base64.b64encode(_pdf_batch_bytes(path, start, end)).decode("ascii")
        messages = [{
            "role": "user",
            "content": [
                {"type": "text", "text": _reader_prompt()},
                {"type": "file", "file": {
                    "filename": f"{path.stem}_p{start + 1}-{end}.pdf",
                    "file_data": f"data:application/pdf;base64,{b64}",
                }},
            ],
        }]
        raw = _call(messages, plugins=plugins)
        batch_chunks = [c for c in (
            _normalize(r, page_start=start + 1, page_end=end) for r in raw) if c]
        if batch_chunks:
            return batch_chunks
        log.warning("Native read produced 0 chunks for pages %d-%d; text fallback.",
                    start + 1, end)
    except Exception as exc:  # noqa: BLE001 — never lose the whole doc over one batch
        log.warning("Native read failed for pages %d-%d (%s); using text fallback.",
                    start + 1, end, exc)
    return _chunk_text(_pages_text(path, start, end))


def read_pdf_native(path: Path, on_progress: ProgressCb = None) -> list[Chunk]:
    with fitz.open(str(path)) as d:
        total = d.page_count
    if total == 0:
        raise ValueError("PDF has no pages.")

    batch = max(1, settings.PDF_PAGES_PER_BATCH)
    plugins = [{"id": "file-parser", "pdf": {"engine": settings.PDF_ENGINE}}]
    spans = [(s, min(s + batch, total)) for s in range(0, total, batch)]
    workers = max(1, min(settings.INGEST_CONCURRENCY, len(spans)))

    chunks: list[Chunk] = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        # ex.map preserves page order regardless of completion order.
        for batch_chunks in ex.map(tokens.with_context(
                lambda se: _read_batch(path, se[0], se[1], total, plugins, on_progress)),
                spans):
            chunks.extend(batch_chunks)
    return chunks


# --------------------------------------------------------------------------- #
#  Text chunking (DOCX, and PDF fallback)                                      #
# --------------------------------------------------------------------------- #
def _chunk_text(text: str) -> list[Chunk]:
    text = (text or "").strip()
    if not text:
        return []
    system = TEXT_CHUNK_SYSTEM.format(n=settings.CHUNK_TARGET_WORDS)
    chunks: list[Chunk] = []
    for window in _split_by_size(text, settings.CHUNK_WINDOW_CHARS, settings.CHUNK_WINDOW_OVERLAP):
        try:
            raw = _call([
                {"role": "system", "content": system},
                {"role": "user", "content": f"Text to chunk:\n\n{window}"},
            ])
            part = [c for c in (_normalize(r, page_start=None, page_end=None) for r in raw) if c]
            chunks.extend(part or _deterministic(window))
        except Exception as exc:  # noqa: BLE001
            log.warning("Text chunking failed (%s); using deterministic split.", exc)
            chunks.extend(_deterministic(window))
    return chunks


def _deterministic(text: str) -> list[Chunk]:
    out = []
    for piece in _split_by_size(text, settings.FALLBACK_CHUNK_SIZE, settings.FALLBACK_CHUNK_OVERLAP):
        piece = piece.strip()
        if piece:
            out.append(Chunk(text=piece, title="", summary="", keywords=[],
                             lang=detect_lang(piece), page_start=None, page_end=None))
    return out


# --------------------------------------------------------------------------- #
#  Public entry point                                                          #
# --------------------------------------------------------------------------- #
def read_and_chunk(path: str | Path, file_type: str, on_progress: ProgressCb = None) -> list[Chunk]:
    path = Path(path)

    if file_type == "pdf":
        size_mb = path.stat().st_size / (1024 * 1024)
        if settings.PDF_INGEST_MODE == "native" and size_mb <= settings.PDF_MAX_MB:
            try:
                chunks = read_pdf_native(path, on_progress)
                if chunks:
                    return chunks
                log.warning("Native PDF read produced 0 chunks; falling back to text.")
            except Exception as exc:  # noqa: BLE001
                log.warning("Native PDF read failed (%s); falling back to text.", exc)
        # text mode / fallback
        if on_progress:
            on_progress("extracting", "PyMuPDF text fallback")
        with fitz.open(str(path)) as d:
            full = "\n\n".join(p.get_text("text") for p in d)
        return _chunk_text(full)

    if file_type == "docx":
        if on_progress:
            on_progress("extracting", "DOCX text")
        doc = extract_docx(path)
        if not doc.full_text.strip():
            raise ValueError("No text found in DOCX.")
        return _chunk_text(doc.full_text)

    if file_type == "txt":
        if on_progress:
            on_progress("extracting", "text")
        doc = extract_txt(path)
        return _chunk_text(doc.full_text)

    raise ValueError(f"Unsupported file type for reader: {file_type}")

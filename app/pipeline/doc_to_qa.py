"""Document -> bilingual Q&A knowledge base.

Instead of chunking, the model READS the file in strict detail and writes an
exhaustive set of Q&A units. For every distinct fact a student could ask about
it produces:
  * >= QA_VARIANTS English question paraphrases + a grounded English answer
  * >= QA_VARIANTS Arabic  question paraphrases + a grounded Arabic answer
  * the source page and a short topic

Each question paraphrase is embedded as its own vector pointing at the shared
answer, so any phrasing (Arabic or English, formal or casual) retrieves it.

Answers are strictly grounded in the document — no invented facts.
"""

from __future__ import annotations

import base64
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Optional

import fitz  # PyMuPDF
from openai import APIError
from tenacity import (retry, retry_if_exception_type, stop_after_attempt,
                      wait_exponential)

from ..config import settings
from ..core import tokens
from ..core.llm import ProviderResponseError, first_choice_content, get_client
from ..core.utils import detect_lang, now_iso, stable_id
from .doc_reader import _pages_text, _pdf_batch_bytes  # reuse PDF helpers
from .extract import extract_docx, extract_txt

log = logging.getLogger(__name__)

ProgressCb = Optional[Callable[[str, str], None]]


def _system_prompt() -> str:
    v = max(3, settings.QA_VARIANTS)
    return (
        "You are a senior bilingual (Arabic & English) NLP engineer building a "
        "Q&A knowledge base for University of Jordan students from official "
        "university documents.\n\n"
        "Read the document with STRICT attention to EVERY detail: every fact, "
        "date, deadline, fee, number, name, requirement, step, exception, table "
        "row and footnote.\n\n"
        "Produce an EXHAUSTIVE set of Q&A units — one per distinct piece of "
        "information a student might ask about. For EACH unit:\n"
        f"- Write at least {v} DISTINCT English question paraphrases (vary the "
        "wording, structure and register — formal and casual, direct and "
        "indirect, synonyms).\n"
        f"- Write at least {v} DISTINCT Arabic question paraphrases (Modern "
        "Standard Arabic and the way students really ask; vary phrasing).\n"
        "- Write a precise, COMPLETE English answer and the equivalent precise "
        "Arabic answer (faithful translations of each other).\n"
        "- Ground every answer ONLY in the document. Copy exact numbers, dates, "
        "names and fees. NEVER invent, assume, or add outside knowledge.\n"
        "- Add the source page number and a short topic.\n\n"
        "Be thorough: cover minor details, table rows and footnotes. Prefer many "
        "focused units over a few broad ones. Keep each answer self-contained.\n"
        "- For tables where each row describes an entity (a major, course, fee "
        "item, office...), create ONE unit PER ROW that includes ALL of that "
        "row's columns in the answer (e.g. for a major: its faculty, minimum "
        "admission average, plan hours, and every fee). NEVER skip rows.\n"
        "- ALSO add one COMPREHENSIVE unit per table or coherent section that "
        "answers overview questions (totals AND their full breakdown, complete "
        "lists, all steps of a procedure) in a single answer — students often "
        "ask for 'all requirements', 'the whole plan', or 'how is it "
        "distributed'.\n\n"
        "Return STRICT JSON ONLY, no markdown, exactly:\n"
        '{"qa":[{"topic":"short topic","page":<int>,'
        '"questions_en":["q1","q2","q3","q4"],"answer_en":"...",'
        '"questions_ar":["س1","س2","س3","س4"],"answer_ar":"..."}]}\n'
        'If there is no usable information, return {"qa":[]}.'
    )


_USER_PDF = ("Generate the exhaustive bilingual Q&A knowledge base for the "
             "following University of Jordan document pages.")


# --------------------------------------------------------------------------- #
#  Tolerant JSON parsing (Q&A output can be very large)                        #
# --------------------------------------------------------------------------- #
def _salvage_objects(s: str) -> list[dict]:
    """Extract complete `{...}` objects from a (possibly truncated) JSON array."""
    out: list[dict] = []
    depth = 0
    start: Optional[int] = None
    in_str = False
    esc = False
    for j, ch in enumerate(s):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = j
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start is not None:
                    try:
                        out.append(json.loads(s[start:j + 1]))
                    except json.JSONDecodeError:
                        pass
                    start = None
    return out


def _loads_qa(content: str) -> list[dict]:
    s = (content or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\n?", "", s)
        s = re.sub(r"\n?```$", "", s).strip()
    try:
        data = json.loads(s)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            qa = data.get("qa")
            # `{"qa": null}` and other odd shapes must yield a list, never None.
            return qa if isinstance(qa, list) else []
    except json.JSONDecodeError:
        pass
    # Truncated / messy output -> salvage whatever complete units we can.
    return _salvage_objects(s)


# --------------------------------------------------------------------------- #
#  Model call                                                                  #
# --------------------------------------------------------------------------- #
@retry(retry=retry_if_exception_type((APIError, ProviderResponseError)),
       wait=wait_exponential(multiplier=1, min=2, max=20),
       stop=stop_after_attempt(3), reraise=True)
def _request(messages: list[dict], *, json_mode: bool, plugins: Optional[list]) -> str:
    kwargs: dict = {
        "model": settings.CHUNK_MODEL,
        "messages": messages,
        "temperature": settings.CHUNK_TEMPERATURE,
        "max_tokens": settings.QA_MAX_TOKENS,
    }
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    extra_body: dict = {"usage": {"include": True}}  # ask for the real charged cost
    if plugins:
        extra_body["plugins"] = plugins
    kwargs["extra_body"] = extra_body
    # Q&A generation can emit tens of thousands of tokens -> needs a long
    # per-request timeout (the client default would cut it off mid-answer).
    resp = get_client().chat.completions.create(
        timeout=float(settings.INGEST_TIMEOUT_S), **kwargs)
    content = first_choice_content(resp)
    usage = getattr(resp, "usage", None)
    tokens.record(settings.CHUNK_MODEL, "qa", usage, cost_usd=tokens.usage_cost(usage))
    return content


def _call(messages: list[dict], *, plugins: Optional[list] = None) -> list[dict]:
    try:
        content = _request(messages, json_mode=True, plugins=plugins)
    except APIError as exc:
        if getattr(exc, "status_code", None) in (400, 404, 422):
            log.warning("JSON mode unsupported; retrying Q&A generation without it.")
            content = _request(messages, json_mode=False, plugins=plugins)
        else:
            raise
    units = _loads_qa(content)
    return units if isinstance(units, list) else []


# --------------------------------------------------------------------------- #
#  Unit normalization                                                          #
# --------------------------------------------------------------------------- #
def _clean_list(value, limit: int = 8) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    seen, out = set(), []
    for q in value:
        q = (str(q) if q is not None else "").strip()
        key = q.lower()
        if q and key not in seen:
            seen.add(key)
            out.append(q)
    return out[:limit]


def _normalize_unit(raw: dict, *, page_offset: int, fallback_page: int,
                    span: Optional[int] = None) -> Optional[dict]:
    if not isinstance(raw, dict):
        return None
    ans_en = (raw.get("answer_en") or "").strip()
    ans_ar = (raw.get("answer_ar") or "").strip()
    q_en = _clean_list(raw.get("questions_en"))
    q_ar = _clean_list(raw.get("questions_ar"))
    if not (ans_en or ans_ar) or not (q_en or q_ar):
        return None
    try:
        pg = int(raw.get("page"))
    except (TypeError, ValueError):
        pg = None
    if pg is None:
        page = fallback_page
    elif span is None:
        page = pg if pg > 0 else fallback_page
    # Page-batched PDFs: the model may report the position inside the mini-PDF
    # (1..span) or the printed absolute page number — accept both.
    elif 1 <= pg <= span:
        page = page_offset + pg
    elif page_offset < pg <= page_offset + span:
        page = pg
    else:
        page = fallback_page
    return {
        "topic": (raw.get("topic") or "").strip(),
        "page": page,
        "questions_en": q_en,
        "answer_en": ans_en,
        "questions_ar": q_ar,
        "answer_ar": ans_ar,
    }


# --------------------------------------------------------------------------- #
#  PDF (native, page-batched) and text paths                                   #
# --------------------------------------------------------------------------- #
def _qa_batch(path: Path, start: int, end: int, total: int,
              plugins: list, on_progress: ProgressCb) -> list[dict]:
    """Generate Q&A units for PDF pages [start, end) — one worker task."""
    if on_progress:
        on_progress("reading", f"Q&A from pages {start + 1}-{end} of {total}")
    try:
        b64 = base64.b64encode(_pdf_batch_bytes(path, start, end)).decode("ascii")
        messages = [
            {"role": "system", "content": _system_prompt()},
            {"role": "user", "content": [
                {"type": "text", "text": _USER_PDF},
                {"type": "file", "file": {
                    "filename": f"{path.stem}_p{start + 1}-{end}.pdf",
                    "file_data": f"data:application/pdf;base64,{b64}",
                }},
            ]},
        ]
        raw_units = _call(messages, plugins=plugins)
        units = []
        for ru in raw_units:
            u = _normalize_unit(ru, page_offset=start, fallback_page=start + 1,
                                span=end - start)
            if u:
                units.append(u)
        if units:
            return units
        log.warning("Q&A native read produced 0 units for pages %d-%d.", start + 1, end)
    except Exception as exc:  # noqa: BLE001 - try a text fallback for these pages
        log.warning("Q&A native read failed for pages %d-%d (%s); text fallback.",
                    start + 1, end, exc)
    # Fallback only when the pages actually carry text worth another LLM call.
    text = _pages_text(path, start, end)
    if len(text.strip()) < 200:
        return []
    return _text_to_qa(text, on_progress=None, base_page=start + 1)


def _pdf_to_qa(path: Path, on_progress: ProgressCb) -> list[dict]:
    with fitz.open(str(path)) as d:
        total = d.page_count
    if total == 0:
        raise ValueError("PDF has no pages.")
    batch = max(1, settings.QA_PAGES_PER_BATCH)
    plugins = [{"id": "file-parser", "pdf": {"engine": settings.PDF_ENGINE}}]
    spans = [(s, min(s + batch, total)) for s in range(0, total, batch)]
    workers = max(1, min(settings.INGEST_CONCURRENCY, len(spans)))
    units: list[dict] = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        # ex.map preserves page order regardless of completion order.
        for batch_units in ex.map(tokens.with_context(
                lambda se: _qa_batch(path, se[0], se[1], total, plugins, on_progress)),
                spans):
            units.extend(batch_units)
    return units


def _text_to_qa(text: str, on_progress: ProgressCb, base_page: int = 0) -> list[dict]:
    text = (text or "").strip()
    if not text:
        return []
    units: list[dict] = []
    # One call for reasonably sized text; window very large inputs.
    windows = [text]
    if len(text) > settings.CHUNK_WINDOW_CHARS:
        from .chunker import _split_by_size
        windows = _split_by_size(text, settings.CHUNK_WINDOW_CHARS, settings.CHUNK_WINDOW_OVERLAP)
    for w in windows:
        if on_progress:
            on_progress("reading", "Q&A from text")
        try:
            raw_units = _call([
                {"role": "system", "content": _system_prompt()},
                {"role": "user", "content": f"Document text:\n\n{w}"},
            ])
            for ru in raw_units:
                u = _normalize_unit(ru, page_offset=0, fallback_page=base_page or 0)
                if u:
                    if base_page:
                        u["page"] = base_page
                    units.append(u)
        except Exception as exc:  # noqa: BLE001
            log.warning("Q&A text generation failed (%s).", exc)
    return units


def generate_qa(path: str | Path, file_type: str, on_progress: ProgressCb = None) -> list[dict]:
    path = Path(path)
    if file_type == "pdf":
        size_mb = path.stat().st_size / (1024 * 1024)
        if settings.PDF_INGEST_MODE == "native" and size_mb <= settings.PDF_MAX_MB:
            return _pdf_to_qa(path, on_progress)
        with fitz.open(str(path)) as d:
            return _text_to_qa("\n\n".join(p.get_text("text") for p in d), on_progress)
    if file_type == "docx":
        doc = extract_docx(path)
        if not doc.full_text.strip():
            raise ValueError("No text found in DOCX.")
        return _text_to_qa(doc.full_text, on_progress)
    if file_type == "txt":
        doc = extract_txt(path)
        return _text_to_qa(doc.full_text, on_progress)
    raise ValueError(f"Unsupported file type for Q&A generation: {file_type}")


# --------------------------------------------------------------------------- #
#  Q&A units -> vector records (one per question paraphrase, bilingual)        #
# --------------------------------------------------------------------------- #
def qa_units_to_prepared(units: list[dict], *, doc_id: int, file_hash: str,
                         filename: str, source_label: str, default_date: str) -> list[dict]:
    prepared: list[dict] = []
    ingested = now_iso()
    for ui, unit in enumerate(units):
        page = unit.get("page")
        topic = unit.get("topic", "")
        for lang, questions, answer, qp, ap in (
            ("en", unit.get("questions_en") or [], unit.get("answer_en", ""), "Q", "A"),
            ("ar", unit.get("questions_ar") or [], unit.get("answer_ar", ""), "س", "ج"),
        ):
            answer = (answer or "").strip()
            if not answer:
                continue

            def meta(question: str) -> dict:
                return {
                    "doc_id": doc_id,
                    "file_hash": file_hash,
                    "filename": filename,
                    "source_label": source_label,
                    "type": "qa",
                    "lang": lang,
                    "doc_date": default_date,
                    "ingested_at": ingested,
                    "question": question,
                    "answer": answer,
                    "topic": topic,
                    # Unit index: lets retrieval collapse the paraphrase fan-out
                    # back to one context entry per fact.
                    "unit": ui,
                    "page_start": page,
                    "page_end": page,
                }

            first_q = ""
            for qi, q in enumerate(questions):
                q = (q or "").strip()
                if not q:
                    continue
                first_q = first_q or q
                prepared.append({
                    "id": stable_id(file_hash, "qa", lang, str(ui), str(qi)),
                    "embed_text": q,
                    "document": f"{qp}: {q}\n{ap}: {answer}",
                    "metadata": meta(q),
                })

            # Also index the ANSWER content itself, so content-style queries
            # ("tuition for engineering", course names, fees) match even when
            # they don't resemble any generated question phrasing.
            answer_doc = (f"{qp}: {first_q}\n{ap}: {answer}" if first_q
                          else (f"{topic}\n{answer}" if topic else answer))
            prepared.append({
                "id": stable_id(file_hash, "qa", lang, str(ui), "ans"),
                "embed_text": f"{topic}\n{answer}" if topic else answer,
                "document": answer_doc,
                "metadata": meta(first_q),
            })
    return prepared

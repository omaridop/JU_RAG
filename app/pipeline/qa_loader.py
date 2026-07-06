"""Loader for ready-made Q&A JSON files (NO reader-model step — embed & store).

A JSON file uploaded to the panel is never sent through the document-reading
model; it is embedded and stored directly. Two shapes are accepted:

1. **Workflow Q&A bank** (``"qa"`` array) — the SAME bilingual format the
   ingestion workflow itself produces (see ``data/processed/*.qa.json``). This
   is the format to hand to Claude when pre-building a knowledge base from a
   PDF offline::

       {
         "metadata": {"source": "...", "date": "YYYY-MM-DD"},   // optional
         "qa": [
           {"topic": "...", "page": 1,
            "questions_en": ["q1", "q2", "q3", "q4"], "answer_en": "...",
            "questions_ar": ["س1", "س2", "س3", "س4"], "answer_ar": "..."}
         ]
       }

   Each question paraphrase becomes its own vector pointing at the shared
   answer, plus an answer-content vector — identical to the workflow output
   (handled by ``doc_to_qa.qa_units_to_prepared``).

2. **Curated Q&A pairs** (``"qa_pairs"`` array) — the simpler legacy shape
   (see ``data/qa/sample_qa.json``)::

       {
         "metadata": {"source": "...", "category": "...", "language": "ar|en|bilingual",
                      "date": "YYYY-MM-DD", "author": "..."},
         "qa_pairs": [
           {"question": "...", "answer": "...", "language": "ar|en",
            "tags": ["..."], "date": "YYYY-MM-DD"}  // date optional, overrides file date
         ]
       }

   Each pair becomes one vector record: we embed "question + answer" and store
   a formatted "Q/A" document for the LLM context.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..core.utils import detect_lang, now_iso, stable_id, to_iso


def load_qa_file(path: str | Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict) or not (
            isinstance(data.get("qa"), list) or isinstance(data.get("qa_pairs"), list)):
        raise ValueError(
            "QA file must be a JSON object with a 'qa' array (workflow format) "
            "or a 'qa_pairs' array (curated format).")
    return data


def detect_qa_format(data: dict) -> str:
    """Return ``'units'`` (workflow Q&A bank) or ``'pairs'`` (curated Q&A)."""
    if isinstance(data, dict) and isinstance(data.get("qa"), list):
        return "units"
    if isinstance(data, dict) and isinstance(data.get("qa_pairs"), list):
        return "pairs"
    raise ValueError("Unrecognised QA JSON: need a 'qa' or 'qa_pairs' array.")


def _clean_questions(value, limit: int = 12) -> list[str]:
    """Dedupe + trim a list of question paraphrases (a bare string is accepted)."""
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for q in value:
        q = (str(q).strip() if q is not None else "")
        key = q.lower()
        if q and key not in seen:
            seen.add(key)
            out.append(q)
    return out[:limit]


def normalize_qa_units(data: dict) -> list[dict]:
    """Sanitize a hand/Claude-authored ``"qa"`` bank into clean workflow units.

    Produces exactly the unit shape ``doc_to_qa.qa_units_to_prepared`` expects
    (topic, page, questions_en/ar, answer_en/ar), so a ready-made JSON file is
    embedded and stored on the SAME path as the reader-model output — just
    without the reader-model call.
    """
    out: list[dict] = []
    for raw in data.get("qa") or []:
        if not isinstance(raw, dict):
            continue
        ans_en = (raw.get("answer_en") or "").strip()
        ans_ar = (raw.get("answer_ar") or "").strip()
        q_en = _clean_questions(raw.get("questions_en"))
        q_ar = _clean_questions(raw.get("questions_ar"))
        # Keep a unit only if at least one answer AND at least one question
        # exist (mirrors doc_to_qa._normalize_unit).
        if not (ans_en or ans_ar) or not (q_en or q_ar):
            continue
        try:
            page = int(raw.get("page"))
        except (TypeError, ValueError):
            page = None
        out.append({
            "topic": (raw.get("topic") or "").strip(),
            "page": page,
            "questions_en": q_en,
            "answer_en": ans_en,
            "questions_ar": q_ar,
            "answer_ar": ans_ar,
        })
    return out


def qa_to_prepared(data: dict, *, doc_id: int, file_hash: str, filename: str,
                   default_date: str) -> list[dict]:
    """Turn a loaded QA file into prepared vector records (no embeddings yet)."""
    meta_top = data.get("metadata", {}) or {}
    file_lang = meta_top.get("language")
    file_date = to_iso(meta_top.get("date")) or default_date
    source_label = meta_top.get("source") or filename
    category = meta_top.get("category", "")

    prepared: list[dict] = []
    for i, pair in enumerate(data.get("qa_pairs", [])):
        q = (pair.get("question") or "").strip()
        a = (pair.get("answer") or "").strip()
        if not q or not a:
            continue
        lang = pair.get("language") or (file_lang if file_lang in ("ar", "en") else None) \
            or detect_lang(f"{q} {a}")
        date = to_iso(pair.get("date")) or file_date
        tags = pair.get("tags") or []
        prepared.append({
            "id": stable_id(file_hash, "qa", str(i)),
            "embed_text": f"{q}\n{a}",
            "document": f"Q: {q}\nA: {a}",
            "metadata": {
                "doc_id": doc_id,
                "file_hash": file_hash,
                "filename": filename,
                "source_label": source_label,
                "category": category,
                "type": "qa",
                "lang": lang,
                "doc_date": date,
                "ingested_at": now_iso(),
                "question": q,
                "tags": tags,
            },
        })
    return prepared

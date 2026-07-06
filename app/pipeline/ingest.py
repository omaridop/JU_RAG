"""Ingestion orchestrator: extract -> chunk -> embed -> store.

Key behaviours requested by the spec:
  * **Incremental** — a file is identified by its SHA-256; one already ingested
    is skipped, and new files are *added on top of* the existing vector store
    (never a full rebuild).
  * **Dated** — every record carries ``doc_date`` (effective date) and
    ``ingested_at`` (system clock). Bulk/CLI ingest defaults ``doc_date`` to the
    runtime; admin uploads pass an explicit, admin-selected date.
  * **Resilient** — chunking falls back to a deterministic splitter; failures
    mark the document row as ``error`` rather than crashing the app.
  * **Auditable** — the chunk set is also written to ``data/processed`` as JSON.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from pathlib import Path
from typing import Callable, Optional

from ..config import settings
from ..core import db, tokens
from ..core.utils import detect_lang, file_sha256, now_iso, stable_id, to_iso
from .chunker import Chunk
from .doc_reader import read_and_chunk
from .doc_to_qa import generate_qa, qa_units_to_prepared
from .embed import embed_texts
from .qa_loader import (detect_qa_format, load_qa_file, normalize_qa_units,
                        qa_to_prepared)
from .vectorstore import get_store

log = logging.getLogger(__name__)

ProgressCb = Optional[Callable[[str, str], None]]


def _emit(cb: ProgressCb, stage: str, detail: str = "") -> None:
    if cb:
        try:
            cb(stage, detail)
        except Exception:  # noqa: BLE001 - progress reporting must never break ingest
            pass
    log.info("[ingest] %s %s", stage, detail)


# Stages mirrored into documents.status so the admin panel shows live progress.
_DB_STAGES = {"reading", "extracting", "loading", "embedding", "storing"}


def _make_progress(doc_id: int, on_progress: ProgressCb) -> Callable[[str, str], None]:
    """Build a callback that fans progress out to the caller AND the DB row."""
    def cb(stage: str, detail: str = "") -> None:
        _emit(on_progress, stage, detail)
        try:
            if stage in _DB_STAGES:
                db.update_document(doc_id, status=stage, progress=detail)
            else:
                db.update_document(doc_id, progress=f"{stage}: {detail}" if detail else stage)
        except Exception:  # noqa: BLE001 - progress reporting must never break ingest
            log.debug("Progress DB update failed", exc_info=True)
    return cb


def _detect_type(path: Path) -> str:
    ext = path.suffix.lower().lstrip(".")
    if ext == "pdf":
        return "pdf"
    if ext in ("docx", "doc"):
        return "docx"
    if ext in ("txt", "md", "text"):
        return "txt"
    if ext == "json":
        return "qa"
    raise ValueError(f"Unsupported file type: .{ext}")


def _chunks_to_prepared(chunks: list[Chunk], *, doc_id: int, file_hash: str,
                        filename: str, source_label: str, doc_date: str) -> list[dict]:
    prepared = []
    ingested = now_iso()
    for i, ch in enumerate(chunks):
        embed_text = f"{ch.title}\n{ch.text}" if ch.title else ch.text
        prepared.append({
            "id": stable_id(file_hash, "chunk", str(i)),
            "embed_text": embed_text,
            "document": ch.text,
            "metadata": {
                "doc_id": doc_id,
                "file_hash": file_hash,
                "filename": filename,
                "source_label": source_label,
                "type": "chunk",
                "lang": ch.lang,
                "doc_date": doc_date,
                "ingested_at": ingested,
                "title": ch.title,
                "summary": ch.summary,
                "keywords": ch.keywords,
                "page_start": ch.page_start,
                "page_end": ch.page_end,
            },
        })
    return prepared


def _majority_lang(prepared: list[dict]) -> str:
    langs = [p["metadata"].get("lang") for p in prepared if p["metadata"].get("lang")]
    if not langs:
        return "en"
    return Counter(langs).most_common(1)[0][0]


def _write_qa_artifact(doc_row: dict, units: list[dict]) -> None:
    """Persist the generated bilingual Q&A set as JSON (reviewable/editable)."""
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in doc_row["filename"])
    out_path = settings.PROCESSED_DIR / f"{doc_row['id']}_{safe}.qa.json"
    payload = {
        "document": {
            "id": doc_row["id"],
            "filename": doc_row["filename"],
            "file_type": doc_row["file_type"],
            "file_hash": doc_row["file_hash"],
            "source_label": doc_row.get("source_label"),
            "doc_date": doc_row["doc_date"],
            "ingested_at": doc_row["ingested_at"],
            "qa_units": len(units),
        },
        "qa": units,
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def _write_artifact(doc_row: dict, prepared: list[dict]) -> None:
    """Persist the chunk set as JSON (the 'convert to JSON' deliverable)."""
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in doc_row["filename"])
    out_path = settings.PROCESSED_DIR / f"{doc_row['id']}_{safe}.json"
    payload = {
        "document": {
            "id": doc_row["id"],
            "filename": doc_row["filename"],
            "file_type": doc_row["file_type"],
            "file_hash": doc_row["file_hash"],
            "source_label": doc_row.get("source_label"),
            "doc_date": doc_row["doc_date"],
            "ingested_at": doc_row["ingested_at"],
            "chunk_count": len(prepared),
        },
        "records": [
            {"id": p["id"], "document": p["document"], "metadata": p["metadata"]}
            for p in prepared
        ],
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


# --------------------------------------------------------------------------- #
#  Public API                                                                 #
# --------------------------------------------------------------------------- #
def ingest_file(path: str | Path, *, doc_date: Optional[str] = None,
                source_label: str = "", uploaded_by: str = "",
                file_type: Optional[str] = None, force: bool = False,
                display_name: Optional[str] = None,
                on_progress: ProgressCb = None) -> dict:
    """Ingest a single file. Returns a result dict.

    ``doc_date``: the effective date for recency. If falsy, defaults to the
    runtime clock (used for the initial/bulk ingest). Admin uploads pass an
    explicit value selected in the panel.
    ``display_name``: human-facing filename (e.g. the original Arabic upload
    name); the stored path may be an ASCII-sanitized version of it.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)

    file_type = file_type or _detect_type(path)
    file_hash = file_sha256(path)
    doc_date_iso = to_iso(doc_date) or now_iso()       # <-- runtime default

    existing = db.get_document_by_hash(file_hash)
    # Keep the human-facing name the admin saw at upload time unless the
    # caller explicitly overrides it (stored names may be ASCII-sanitized).
    prior_name = (existing or {}).get("filename")
    filename = display_name or prior_name or path.name
    source_label = source_label or (existing or {}).get("source_label") or path.stem
    if existing and existing["status"] == "done" and not force:
        _emit(on_progress, "skipped", f"already ingested as #{existing['id']}")
        return {"status": "skipped", "doc_id": existing["id"],
                "filename": filename, "chunk_count": existing["chunk_count"]}

    if existing:
        doc_id = existing["id"]
        get_store().delete_doc(doc_id)  # clear stale vectors before re-ingest
        db.update_document(doc_id, status="processing", error=None, progress=None,
                           doc_date=doc_date_iso, source_label=source_label,
                           filename=filename, stored_path=str(path))
    else:
        doc_id = db.add_document(
            filename=filename, stored_path=str(path), file_hash=file_hash,
            file_type=file_type, source_label=source_label, doc_date=doc_date_iso,
            uploaded_by=uploaded_by, status="processing",
        )

    stage = "preparing"
    usage_ctx = tokens.set_context(doc_id=doc_id)  # attribute all token usage to this doc
    db.delete_estimated_usage(doc_id)  # real tracking now supersedes any backfilled estimate
    try:
        qa_units = None
        cb = _make_progress(doc_id, on_progress)
        if file_type == "qa":
            # A ready-made JSON Q&A file: embed & store directly, NO reader model.
            stage = "loading"
            cb("loading", path.name)
            data = load_qa_file(path)
            if detect_qa_format(data) == "units":
                # Workflow bilingual bank (questions_en/ar + answer_en/ar) —
                # fan each paraphrase out exactly like the reader-model output.
                qa_units = normalize_qa_units(data)
                if not qa_units:
                    raise ValueError("The 'qa' array has no usable units "
                                     "(each unit needs questions and an answer).")
                cb("loading", f"{len(qa_units)} Q&A unit(s) from JSON")
                prepared = qa_units_to_prepared(
                    qa_units, doc_id=doc_id, file_hash=file_hash, filename=filename,
                    source_label=source_label, default_date=doc_date_iso)
            else:
                # Curated single-answer Q&A pairs (legacy shape).
                prepared = qa_to_prepared(data, doc_id=doc_id, file_hash=file_hash,
                                          filename=filename, default_date=doc_date_iso)
        elif settings.INGEST_STRATEGY == "qa":
            # The model reads the file and writes a bilingual Q&A knowledge base.
            stage = "reading"
            cb("reading", f"{path.name} -> Q&A via {settings.CHUNK_MODEL}")
            qa_units = generate_qa(path, file_type, on_progress=cb)
            prepared = qa_units_to_prepared(
                qa_units, doc_id=doc_id, file_hash=file_hash, filename=filename,
                source_label=source_label, default_date=doc_date_iso)
        else:
            # Classic semantic chunking (the model still reads the file directly).
            stage = "reading"
            cb("reading", f"{path.name} via {settings.CHUNK_MODEL}")
            chunks = read_and_chunk(path, file_type, on_progress=cb)
            prepared = _chunks_to_prepared(
                chunks, doc_id=doc_id, file_hash=file_hash, filename=filename,
                source_label=source_label, doc_date=doc_date_iso)

        if not prepared:
            raise ValueError("Produced 0 records (the document may be empty or unreadable).")

        stage = "embedding"
        cb("embedding", f"0/{len(prepared)} record(s)")
        vectors = embed_texts(
            [p["embed_text"] for p in prepared],
            on_progress=lambda done, total: cb("embedding", f"{done}/{total} record(s)"),
        )

        records = [
            {"id": p["id"], "embedding": v, "document": p["document"], "metadata": p["metadata"]}
            for p, v in zip(prepared, vectors)
        ]
        stage = "storing"
        cb("storing", f"{len(records)} record(s)")
        get_store().add_records(records)

        lang = _majority_lang(prepared)
        db.update_document(doc_id, status="done", chunk_count=len(prepared), lang=lang,
                           error=None, progress=None)

        doc_row = db.get_document(doc_id)
        if qa_units is not None:
            _write_qa_artifact(doc_row, qa_units)
        else:
            _write_artifact(doc_row, prepared)

        _emit(on_progress, "done", f"{len(records)} record(s) stored")
        return {"status": "done", "doc_id": doc_id, "filename": filename,
                "chunk_count": len(records), "lang": lang}

    except Exception as exc:  # noqa: BLE001 - record the failure, don't crash
        log.exception("Ingestion failed for %s (stage: %s)", path.name, stage)
        msg = f"[{stage}] {exc}"
        db.update_document(doc_id, status="error", error=msg[:500], progress=None)
        _emit(on_progress, "error", msg)
        return {"status": "error", "doc_id": doc_id, "filename": filename, "error": msg}
    finally:
        tokens.reset_context(usage_ctx)


def ingest_directory(directory: str | Path, *, doc_date: Optional[str] = None,
                     on_progress: ProgressCb = None) -> list[dict]:
    """Ingest every supported file in a directory (PDF/DOCX/JSON)."""
    directory = Path(directory)
    results = []
    for p in sorted(directory.iterdir()):
        if p.is_file() and p.suffix.lower().lstrip(".") in ("pdf", "docx", "doc", "json"):
            results.append(ingest_file(p, doc_date=doc_date, on_progress=on_progress))
    return results

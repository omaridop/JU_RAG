"""One-time backfill: estimate token usage/cost for documents ingested before
token tracking existed, so the admin Cost page reflects prior work.

Estimates are clearly flagged (``estimated=1``) and use the current (real)
price table. Q&A-generation OUTPUT — the dominant cost — is estimated directly
from the saved Q&A artifacts; input/embedding are estimated from the source
text and the produced records.

Run once:  python scripts/backfill_token_usage.py
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass

import fitz  # PyMuPDF

from app.config import settings
from app.core import db, tokens
from app.core.utils import detect_lang  # noqa: F401 (ensures app imports cleanly)
from app.pipeline.doc_to_qa import qa_units_to_prepared
from app.pipeline.extract import extract_docx, extract_txt

CHUNK_MODEL = settings.CHUNK_MODEL
EMBED_MODEL = settings.EMBED_MODEL


def _source_text(doc: dict) -> str:
    p = Path(doc["stored_path"])
    if not p.exists():
        return ""
    ft = doc["file_type"]
    try:
        if ft == "pdf":
            with fitz.open(str(p)) as d:
                return "\n".join(pg.get_text("text") for pg in d)
        if ft == "docx":
            return extract_docx(p).full_text
        if ft == "txt":
            return extract_txt(p).full_text
    except Exception as exc:  # noqa: BLE001
        print(f"   ! could not read source ({exc})")
    return ""


def _artifact(doc_id: int) -> Path | None:
    qa = sorted(settings.PROCESSED_DIR.glob(f"{doc_id}_*.qa.json"))
    if qa:
        return qa[0]
    other = sorted(p for p in settings.PROCESSED_DIR.glob(f"{doc_id}_*.json")
                   if not p.name.endswith(".qa.json"))
    return other[0] if other else None


def _already_tracked(doc_id: int) -> bool:
    con = sqlite3.connect(str(settings.DB_PATH))
    n = con.execute("SELECT COUNT(*) FROM token_usage WHERE doc_id=?", (doc_id,)).fetchone()[0]
    con.close()
    return n > 0


def main() -> int:
    db.init_db()
    try:
        live = tokens.refresh_pricing_from_openrouter()
        print(f"Refreshed live prices for {len(live)} model(s) from OpenRouter.\n")
    except Exception as exc:  # noqa: BLE001
        print(f"(Could not refresh live prices: {exc}; using stored/default.)\n")
    pricing = tokens.get_pricing()
    est = tokens.estimate_tokens

    done = [d for d in db.list_documents() if d["status"] == "done"]
    print(f"Backfilling estimates for {len(done)} completed document(s):\n")
    inserted = 0
    for doc in done:
        did = doc["id"]
        if _already_tracked(did):
            print(f"#{did} {doc['filename'][:45]} — already tracked, skipped")
            continue
        art = _artifact(did)
        if not art:
            print(f"#{did} {doc['filename'][:45]} — no artifact, skipped")
            continue
        payload = json.loads(art.read_text(encoding="utf-8"))

        qa_in = qa_out = embed_tok = 0
        if "qa" in payload:                       # model-generated Q&A
            units = payload["qa"]
            qa_out = est(json.dumps(units, ensure_ascii=False))
            qa_in = est(_source_text(doc)) or max(1, qa_out // 4)
            prepared = qa_units_to_prepared(
                units, doc_id=did, file_hash="x", filename="x",
                source_label="x", default_date="2026-01-01")
            embed_tok = sum(est(p["embed_text"]) for p in prepared)
        else:                                     # loaded records (chunk/QA file)
            for rec in payload.get("records", []):
                embed_tok += est(rec.get("document", ""))

        if qa_in or qa_out:
            db.add_token_usage(
                model=CHUNK_MODEL, operation="qa", prompt_tokens=qa_in,
                completion_tokens=qa_out,
                cost_usd=tokens.cost(CHUNK_MODEL, qa_in, qa_out, pricing),
                estimated=1, doc_id=did)
            inserted += 1
        if embed_tok:
            db.add_token_usage(
                model=EMBED_MODEL, operation="embed", prompt_tokens=embed_tok,
                completion_tokens=0,
                cost_usd=tokens.cost(EMBED_MODEL, embed_tok, 0, pricing),
                estimated=1, doc_id=did)
            inserted += 1
        c = (tokens.cost(CHUNK_MODEL, qa_in, qa_out, pricing)
             + tokens.cost(EMBED_MODEL, embed_tok, 0, pricing))
        print(f"#{did} {doc['filename'][:45]} — qa(in {qa_in:,}/out {qa_out:,}) "
              f"embed {embed_tok:,} => ~${c:.4f}")

    print(f"\nDone. Inserted {inserted} estimated usage row(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

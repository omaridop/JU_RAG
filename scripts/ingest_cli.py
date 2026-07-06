"""Bulk / initial ingestion from the command line.

This is the "first run" path: drop your PDFs, DOCX, and Q&A JSON files into a
folder and ingest them all at once. Unless you pass --date, each file's
effective date defaults to the RUNTIME clock (as specified).

Examples
--------
    # Ingest everything in data/uploads and data/qa (default):
    python scripts/ingest_cli.py

    # Ingest a specific folder or files:
    python scripts/ingest_cli.py "C:/docs/handbook.pdf" "C:/docs/faqs"

    # Force a re-ingest and set an explicit effective date:
    python scripts/ingest_cli.py data/uploads --date 2026-01-15 --force
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Make the project root importable when run as `python scripts/ingest_cli.py`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass

from app.config import settings           # noqa: E402
from app.core import db                   # noqa: E402
from app.pipeline.ingest import ingest_file  # noqa: E402

SUPPORTED = (".pdf", ".docx", ".doc", ".json")


def _iter_files(paths: list[Path]):
    for p in paths:
        if p.is_dir():
            yield from (f for f in sorted(p.iterdir())
                        if f.is_file() and f.suffix.lower() in SUPPORTED)
        elif p.is_file() and p.suffix.lower() in SUPPORTED:
            yield p
        else:
            print(f"  ! skipping (unsupported/not found): {p}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Bulk-ingest documents into the RAG store.")
    ap.add_argument("paths", nargs="*", help="Files or folders to ingest.")
    ap.add_argument("--date", default=None,
                    help="Effective date (YYYY-MM-DD). Default: runtime now.")
    ap.add_argument("--force", action="store_true", help="Re-ingest even if unchanged.")
    args = ap.parse_args()

    if not settings.is_configured():
        print("ERROR: OPENROUTER_API_KEY is not set. Create a .env file first "
              "(copy .env.example).")
        return 2

    db.init_db()

    targets = [Path(p) for p in args.paths] or [settings.UPLOAD_DIR, settings.QA_DIR]
    files = list(_iter_files(targets))
    if not files:
        print("No supported files found. Put PDFs/DOCX in data/uploads and "
              "Q&A JSON in data/qa, or pass paths explicitly.")
        return 1

    print(f"Ingesting {len(files)} file(s). Effective date: "
          f"{args.date or 'runtime now'}\n")

    def progress(stage: str, detail: str) -> None:
        print(f"     · {stage}: {detail}")

    ok = skipped = errors = 0
    for f in files:
        print(f"→ {f.name}")
        res = ingest_file(f, doc_date=args.date, source_label=f.stem,
                          uploaded_by="cli", force=args.force, on_progress=progress)
        status = res.get("status")
        if status == "done":
            ok += 1
            print(f"   ✓ {res['chunk_count']} record(s) stored\n")
        elif status == "skipped":
            skipped += 1
            print(f"   = already ingested (#{res['doc_id']}); use --force to redo\n")
        else:
            errors += 1
            print(f"   ✗ error: {res.get('error')}\n")

    print(f"Done. {ok} ingested, {skipped} skipped, {errors} failed.")
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

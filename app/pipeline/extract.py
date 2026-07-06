"""Text extraction from PDF (PyMuPDF), DOCX (python-docx) and plain text.

PyMuPDF is used for PDFs because it has excellent Unicode/Arabic handling and
preserves logical reading order well.

DOCX extraction is **comprehensive and order-preserving**: paragraphs and
tables are emitted in true document order, and text is harvested from every
place Word hides it — runs, hyperlinks, fields, **text boxes**, headers,
footers, footnotes and endnotes. (The naive ``Document.paragraphs`` +
``Document.tables`` approach loses ordering and silently drops all of those.)
"""

from __future__ import annotations

import logging
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import fitz  # PyMuPDF
from docx import Document as DocxDocument
from docx.oxml.ns import qn

log = logging.getLogger(__name__)

_WS = re.compile(r"[ \t ]+")
_BLANKS = re.compile(r"\n{3,}")

# python-docx namespaced tags we care about while walking the XML tree.
_T = qn("w:t")
_TAB = qn("w:tab")
_BR = qn("w:br")
_CR = qn("w:cr")
_P = qn("w:p")
_TBL = qn("w:tbl")
_TR = qn("w:tr")
_TC = qn("w:tc")


@dataclass
class Page:
    page: int | None        # 1-based page number, or None (DOCX / text)
    text: str


@dataclass
class ExtractedDoc:
    filename: str
    file_type: str          # 'pdf' | 'docx' | 'txt'
    pages: list[Page] = field(default_factory=list)

    @property
    def full_text(self) -> str:
        return "\n\n".join(p.text for p in self.pages if p.text.strip())

    @property
    def page_count(self) -> int:
        return len(self.pages)

    @property
    def char_count(self) -> int:
        return sum(len(p.text) for p in self.pages)


def _clean(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _WS.sub(" ", text)
    text = _BLANKS.sub("\n\n", text)
    # Strip trailing spaces per line.
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    return text.strip()


# --------------------------------------------------------------------------- #
#  PDF                                                                         #
# --------------------------------------------------------------------------- #
def extract_pdf(path: str | Path) -> ExtractedDoc:
    path = Path(path)
    pages: list[Page] = []
    with fitz.open(str(path)) as doc:
        for i, page in enumerate(doc, start=1):
            text = _clean(page.get_text("text"))
            if text:
                pages.append(Page(page=i, text=text))
    return ExtractedDoc(filename=path.name, file_type="pdf", pages=pages)


# --------------------------------------------------------------------------- #
#  DOCX                                                                        #
# --------------------------------------------------------------------------- #
def _node_text(el) -> str:
    """All text under an element, in document order.

    Walking ``el.iter()`` reaches text wherever Word stores it — ordinary
    runs, hyperlink runs, field results, smart tags and (crucially) the
    paragraphs nested inside **text boxes** (``w:txbxContent``) — because they
    are all descendants of the element.
    """
    out: list[str] = []
    for node in el.iter():
        tag = node.tag
        if tag == _T:
            out.append(node.text or "")
        elif tag == _TAB:
            out.append(" ")
        elif tag in (_BR, _CR):
            out.append("\n")
    return "".join(out)


def _render_table(tbl_el) -> list[str]:
    """Render a table as ' | '-joined rows, preserving row order.

    Nested tables inside a cell are flattened into that cell's text (their
    content is still captured, just without inner pipes).
    """
    rows: list[str] = []
    for tr in tbl_el.findall(_TR):
        cells = []
        for tc in tr.findall(_TC):
            cells.append(" ".join(_node_text(tc).split()))
        if any(c.strip() for c in cells):
            rows.append(" | ".join(cells))
    return rows


def _body_blocks(doc) -> list[str]:
    """Paragraphs and tables of the main body, in true document order."""
    parts: list[str] = []
    for child in doc.element.body.iterchildren():
        if child.tag == _P:
            t = _node_text(child).strip()
            if t:
                parts.append(t)
        elif child.tag == _TBL:
            parts.extend(_render_table(child))
    return parts


def _headers_footers(doc) -> list[str]:
    """Unique header/footer text across all sections (boilerplate deduped)."""
    seen: set[str] = set()
    out: list[str] = []
    for section in doc.sections:
        for part in (
            section.first_page_header, section.header, section.even_page_header,
            section.first_page_footer, section.footer, section.even_page_footer,
        ):
            try:
                el = part._element
            except AttributeError:
                continue
            for child in el.iterchildren():
                text = ""
                if child.tag == _P:
                    text = _node_text(child).strip()
                elif child.tag == _TBL:
                    text = " ".join(_render_table(child))
                key = text.strip()
                if key and key not in seen:
                    seen.add(key)
                    out.append(key)
    return out


def _notes_text(path: Path) -> list[str]:
    """Footnote/endnote text, read straight from the package parts."""
    out: list[str] = []
    try:
        with zipfile.ZipFile(str(path)) as z:
            members = set(z.namelist())
            for name in ("word/footnotes.xml", "word/endnotes.xml"):
                if name not in members:
                    continue
                xml = z.read(name).decode("utf-8", "replace")
                # Pull visible text; skip separator/continuation notes.
                for chunk in re.findall(r"<w:t[^>]*>(.*?)</w:t>", xml, flags=re.S):
                    chunk = chunk.strip()
                    if chunk:
                        out.append(chunk)
    except (zipfile.BadZipFile, KeyError):
        pass
    return out


def extract_docx(path: str | Path) -> ExtractedDoc:
    path = Path(path)
    if not zipfile.is_zipfile(str(path)):
        # A legacy binary .doc (or a mislabeled file) is not an OOXML zip.
        raise ValueError(
            "This looks like a legacy Word file (.doc) or it is corrupted. "
            "Please re-save it as .docx in Word (File → Save As → Word Document) "
            "and upload again."
        )
    try:
        doc = DocxDocument(str(path))
    except Exception as exc:  # noqa: BLE001 - surface a clean, actionable message
        raise ValueError(f"Could not open the Word file: {exc}") from exc

    parts = _body_blocks(doc)

    # Supplement with content that lives outside the main story.
    extras: list[str] = []
    for label, items in (("", _headers_footers(doc)), ("", _notes_text(path))):
        extras.extend(items)
    # Only append extras that are not already present in the body.
    body_join = "\n".join(parts)
    for e in extras:
        if e and e not in body_join:
            parts.append(e)

    text = _clean("\n\n".join(parts))
    if not text.strip():
        raise ValueError("No readable text found in the Word file.")
    pages = [Page(page=None, text=text)]
    return ExtractedDoc(filename=path.name, file_type="docx", pages=pages)


# --------------------------------------------------------------------------- #
#  Plain text (.txt / .md) and raw typed input                                #
# --------------------------------------------------------------------------- #
def extract_txt(path: str | Path) -> ExtractedDoc:
    path = Path(path)
    raw = None
    for enc in ("utf-8-sig", "utf-8", "cp1256", "latin-1"):
        try:
            raw = path.read_text(encoding=enc)
            break
        except (UnicodeDecodeError, UnicodeError):
            continue
    if raw is None:
        raw = path.read_bytes().decode("utf-8", "replace")
    text = _clean(raw)
    if not text.strip():
        raise ValueError("The text file is empty.")
    return ExtractedDoc(filename=path.name, file_type="txt",
                        pages=[Page(page=None, text=text)])


def extract_document(path: str | Path) -> ExtractedDoc:
    """Dispatch on file extension. Raises ValueError for unsupported types."""
    path = Path(path)
    ext = path.suffix.lower().lstrip(".")
    if ext == "pdf":
        return extract_pdf(path)
    if ext in ("docx", "doc"):
        return extract_docx(path)
    if ext in ("txt", "md", "text"):
        return extract_txt(path)
    raise ValueError(f"Unsupported file type: .{ext} (PDF, Word, or text only)")

"""Assignment document ingestion: PDF via PyMuPDF, plus plain text.

Pure function, no LLM: reads the document, extracts per-page text, hashes
the file. Anything semantic (requirements, examples, ambiguities) is the
specification agent's job.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from ..core.schemas import AssignmentDoc, PageText

_PDF_SUFFIXES = {".pdf"}
_TEXT_SUFFIXES = {".txt", ".md", ".markdown"}


class IngestionError(ValueError):
    """Raised when an assignment document cannot be ingested."""


def ingest_assignment(path: str | Path) -> AssignmentDoc:
    """Read ``path`` and return an :class:`AssignmentDoc`.

    Raises :class:`IngestionError` when the file is missing, has an
    unsupported type, cannot be parsed, or yields no extractable text
    (e.g. a scanned-image PDF — the hint says so explicitly).
    """
    p = Path(path)
    if not p.is_file():
        raise IngestionError(f"assignment file not found: {p}")
    suffix = p.suffix.lower()
    if suffix in _PDF_SUFFIXES:
        pages = _read_pdf(p)
    elif suffix in _TEXT_SUFFIXES:
        pages = [PageText(page_no=1, text=_read_text(p))]
    else:
        raise IngestionError(
            f"unsupported assignment type {suffix!r} for {p.name}; "
            "use .pdf, .txt or .md"
        )
    text = "\n".join(pg.text for pg in pages).strip()
    if not text:
        raise IngestionError(
            f"no extractable text in {p.name} — if this is a PDF it may be "
            "a scanned image. The agent needs selectable text (or a .txt/.md "
            "transcript) to extract the specification."
        )
    digest = hashlib.sha256(p.read_bytes()).hexdigest()
    return AssignmentDoc(
        filename=p.name,
        sha256=f"sha256:{digest}",
        text=text,
        pages=pages,
    )


def _read_text(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8")
    except OSError as exc:
        raise IngestionError(f"cannot read {p}: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise IngestionError(f"{p.name} is not valid UTF-8 text: {exc}") from exc


def _read_pdf(p: Path) -> list[PageText]:
    try:
        import pymupdf
    except ImportError as exc:
        raise IngestionError(
            "PyMuPDF is not installed — run `pip install -e .` (or "
            "`pip install pymupdf`) to enable PDF ingestion."
        ) from exc
    try:
        doc = pymupdf.open(str(p))
    except Exception as exc:
        raise IngestionError(f"cannot parse PDF {p.name}: {exc}") from exc
    pages: list[PageText] = []
    try:
        for i, page in enumerate(doc, start=1):
            try:
                text = page.get_text() or ""
            except Exception as exc:
                raise IngestionError(
                    f"failed extracting text from page {i} of {p.name}: {exc}"
                ) from exc
            pages.append(PageText(page_no=i, text=text))
    finally:
        doc.close()
    if not pages:
        raise IngestionError(f"PDF {p.name} has no pages")
    return pages

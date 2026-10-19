"""Unit tests for assignment ingestion. Builds tiny PDFs in-test via
PyMuPDF — no fixtures on disk, no network, no LLM."""

from __future__ import annotations

import pytest

from autograder.app.ingestion.pdf import IngestionError, ingest_assignment


def _make_pdf(path, pages: list[str]):
    import pymupdf

    doc = pymupdf.open()
    for text in pages:
        page = doc.new_page()
        page.insert_text((72, 72), text)
    doc.save(str(path))
    doc.close()
    return path


def test_pdf_per_page_text_and_hash(tmp_path):
    pdf = _make_pdf(tmp_path / "a.pdf", ["Add two integers.", "Print the sum."])
    doc = ingest_assignment(pdf)
    assert doc.filename == "a.pdf"
    assert doc.sha256.startswith("sha256:") and len(doc.sha256) == 71
    assert len(doc.pages) == 2
    assert doc.pages[0].page_no == 1
    assert "Add two integers" in doc.pages[0].text
    assert "Print the sum" in doc.pages[1].text
    assert "Add two integers" in doc.text and "Print the sum" in doc.text


def test_empty_text_pdf_raises(tmp_path):
    import pymupdf

    pdf = tmp_path / "blank.pdf"
    doc = pymupdf.open()
    doc.new_page()  # blank page: no text
    doc.save(str(pdf))
    doc.close()
    with pytest.raises(IngestionError, match="scanned image"):
        ingest_assignment(pdf)


def test_missing_file_raises(tmp_path):
    with pytest.raises(IngestionError, match="not found"):
        ingest_assignment(tmp_path / "nope.pdf")


def test_txt_accepted(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("Read n. Print n*n.", encoding="utf-8")
    doc = ingest_assignment(f)
    assert doc.pages[0].page_no == 1
    assert "n*n" in doc.text


def test_unsupported_type_raises(tmp_path):
    f = tmp_path / "a.docx"
    f.write_bytes(b"fake")
    with pytest.raises(IngestionError, match="unsupported"):
        ingest_assignment(f)


def test_hash_stable(tmp_path):
    pdf = _make_pdf(tmp_path / "a.pdf", ["hello"])
    assert ingest_assignment(pdf).sha256 == ingest_assignment(pdf).sha256

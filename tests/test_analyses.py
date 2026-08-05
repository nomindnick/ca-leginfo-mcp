"""Tests for ingest/analyses.py — analysis-lob format detection & extraction.

Dispatch is on file magic, never era (SPEC §6). One real fixture per observed
format lives in tests/fixtures/analyses/; rtf has no observed corpus file so
its magic is exercised synthetically. The .doc conversion test needs
LibreOffice and is skipped when soffice is absent.
"""

from pathlib import Path

import pytest

from ingest import analyses


def _lob(fixtures: Path, name: str) -> bytes:
    return (fixtures / "analyses" / name).read_bytes()


needs_soffice = pytest.mark.skipif(
    not analyses.soffice_available(),
    reason="LibreOffice (soffice) not installed")


# ---------------------------------------------------------------- detect

@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("2025_docx.lob", "docx"),
        ("1997_html.lob", "html"),
        ("2009_doc.lob", "doc"),
        ("1993_text.lob", "text"),
    ],
)
def test_detect_real_fixtures(fixtures, name, expected):
    assert analyses.detect(_lob(fixtures, name)) == expected


def test_detect_rtf_magic_synthetic():
    assert analyses.detect(b"{\\rtf1\\ansi\\deff0 {\\fonttbl} Hello}") == "rtf"


# ---------------------------------------------------------------- extractors

def test_extract_docx_real(fixtures):
    text = analyses.extract_docx(_lob(fixtures, "2025_docx.lob"))
    assert len(text) > 1000
    assert "SB 411" in text
    # Word field instructions are markup, not content.
    assert "MERGEFIELD" not in text
    assert "instrText" not in text
    assert "<w:" not in text


def test_extract_html_real(fixtures):
    text = analyses.extract_html(_lob(fixtures, "1997_html.lob"))
    assert len(text) > 500
    assert "SB 1245" in text
    assert "<html" not in text.lower()
    assert "</" not in text


# ---------------------------------------------------------------- extract_one

def test_extract_one_doc_defers_to_batch(fixtures):
    kind, text = analyses.extract_one(_lob(fixtures, "2009_doc.lob"))
    assert kind == "doc"
    assert text is None


def test_extract_one_docx(fixtures):
    kind, text = analyses.extract_one(_lob(fixtures, "2025_docx.lob"))
    assert kind == "docx"
    assert text is not None and "SB 411" in text


def test_extract_one_html(fixtures):
    kind, text = analyses.extract_one(_lob(fixtures, "1997_html.lob"))
    assert kind == "html"
    assert text is not None and "SB 1245" in text


def test_extract_one_text(fixtures):
    kind, text = analyses.extract_one(_lob(fixtures, "1993_text.lob"))
    assert kind == "text"
    assert text is not None and "BILL ANALYSIS" in text


# ---------------------------------------------------------------- doc batch

@needs_soffice
def test_extract_doc_batch_real(fixtures):
    out = analyses.extract_doc_batch(
        [("analysis_2009", _lob(fixtures, "2009_doc.lob"))])
    assert set(out) == {"analysis_2009"}
    text = out["analysis_2009"]
    assert len(text) > 1000
    assert "AB 2136" in text
    assert "SENATE RULES COMMITTEE" in text

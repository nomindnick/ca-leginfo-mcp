"""Tests for ingest/caml.py — CAML XML flattened to plain text.

Real-data cases use committed lobs in tests/fixtures/lobs/; synthetic cases
pin the specific markup transforms (EnSpace/EmSpace spans, </p> newlines,
deletion-mark PIs).
"""

import re
from pathlib import Path

import pytest

from ingest import caml


def _lob(fixtures: Path, name: str) -> str:
    return (fixtures / "lobs" / name).read_text(encoding="utf-8",
                                                errors="replace")


# ---------------------------------------------------------------- law text

@pytest.mark.parametrize(
    ("name", "phrase"),
    [
        ("law_edc_44955.lob", "certificated employees"),
        ("law_pen_187.lob", "malice"),
        ("law_cons_art1_sec1.lob", "inalienable"),
    ],
)
def test_law_section_text_real_lobs(fixtures, name, phrase):
    text = caml.law_section_text(_lob(fixtures, name))
    assert phrase in text
    # Fully flattened: no tags, PIs, or unresolved entities remain.
    assert "<" not in text
    assert ">" not in text
    assert "&" not in text
    assert text == text.strip()
    assert len(text) > 100


def test_law_section_text_pen187_enspace_becomes_word_break(fixtures):
    """Real EnSpace spans separate the subdivision label from its text."""
    text = caml.law_section_text(_lob(fixtures, "law_pen_187.lob"))
    assert "(a) Murder is the unlawful killing" in text
    assert "(a)Murder" not in text


def test_law_section_text_p_close_becomes_newline():
    out = caml.law_section_text(
        "<caml:Content><p>First sentence.</p><p>Second sentence.</p>"
        "</caml:Content>")
    assert out == "First sentence.\nSecond sentence."


def test_law_section_text_en_and_em_space_spans_become_spaces():
    out = caml.law_section_text(
        '<p>(a)<span class="EnSpace"/>alpha<span class="EmSpace"/>beta</p>')
    assert out == "(a) alpha beta"


# ---------------------------------------------------------------- titles

def test_extract_title_real_amended_bill(fixtures):
    raw = _lob(fixtures, "bill_version_amended.lob")
    # Fixture guard: this lob must keep exercising the deletion-mark path.
    assert "<?xm-deletion_mark" in raw

    title = caml.extract_title(raw)
    assert title is not None
    # Clean prose: no markup, no PI residue.
    assert "<" not in title
    assert ">" not in title
    assert "xm-deletion_mark" not in title
    assert "<?" not in title and "?>" not in title
    assert title == " ".join(title.split())  # normalized whitespace
    assert title.startswith("An act to amend Section 54221")

    # Text that exists only inside deletion-mark PI data attributes is the
    # pre-amendment text and must not leak into the extracted title.
    deleted = [
        " ".join(d.split())
        for d in re.findall(
            r'<\?xm-deletion_mark[^>]*?data="([^"]*)"', raw)
    ]
    assert deleted, "fixture should carry deleted text in PI data attrs"
    for snippet in deleted:
        assert snippet and snippet not in title


def test_extract_title_missing_returns_none():
    assert caml.extract_title(
        "<caml:Bill><caml:Id>SB-1</caml:Id></caml:Bill>") is None
    assert caml.extract_title("") is None


def test_extract_title_synthetic_deletion_mark_stripped():
    xml = ('<caml:Title> An act relating to '
           '<?xm-deletion_mark data="water quality."?>housing.</caml:Title>')
    assert caml.extract_title(xml) == "An act relating to housing."

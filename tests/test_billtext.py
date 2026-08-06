"""Tests for server/billtext.py — SEC-block extraction from flattened bills.

Real-data cases use the § 54953 chain fixtures (tests/fixtures/sec54953/,
see its README): AB 1754's omnibus excerpt, AB 557's four sunset-branch
blocks (incl. the SEC. 1.5 double-joint and a repealed block), and
AB 2302's heading-after-colon flattening. Synthetic cases pin the
individual splitting and intro-parsing rules (SPEC §13).
"""

from pathlib import Path

import pytest

from ingest import caml
from server.billtext import section_blocks, split_blocks


def _flat(fixtures: Path, name: str) -> str:
    return caml.bill_text(
        (fixtures / "sec54953" / name).read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def ab1754(fixtures):
    return section_blocks(_flat(fixtures, "20230AB175497CHP_excerpt.lob"),
                          "Government Code", "54953")


@pytest.fixture(scope="module")
def ab557(fixtures):
    return section_blocks(_flat(fixtures, "20230AB55795CHP.lob"),
                          "Government Code", "54953")


# ------------------------------------------------------------- real lobs

def test_ab1754_three_variant_blocks(ab1754):
    """One chaptered bill carries three parallel versions of § 54953 —
    the sunset branches AB 2449 left behind. All must come back, in print
    order, each with its lineage parenthetical."""
    assert [b.heading for b in ab1754] == ["SEC. 88.", "SEC. 89.", "SEC. 90."]
    assert all(b.action.startswith("amended") for b in ab1754)
    assert [b.lineage for b in ab1754] == [
        "as amended by Section 1 of Chapter 285 of the Statutes of 2022",
        "as amended by Section 2 of Chapter 285 of the Statutes of 2022",
        "as added by Section 3 of Chapter 285 of the Statutes of 2022",
    ]
    for b in ab1754:
        assert b.body.startswith("(a)")
        assert len(b.body) > 4000


def test_ab557_four_blocks_including_double_joint_and_repeal(ab557):
    """AB 557 prints SECTION 1. (no dot after SECTION), the double-joint
    variant SEC. 1.5., SEC. 2., and a repealed block."""
    assert [b.heading for b in ab557] == [
        "SECTION 1.", "SEC. 1.5.", "SEC. 2.", "SEC. 3."]
    assert [b.action for b in ab557][3] == "repealed"
    assert ab557[3].body == ""  # repealed blocks carry no body
    assert ab557[3].lineage == \
        "as added by Section 3 of Chapter 285 of the Statutes of 2022"


def test_lineage_is_whitespace_normalized(ab557):
    """The lob wraps 'Section 1\\nof Chapter 285' mid-parenthetical; the
    lineage key must come back flowed — it is matched against citations
    in later bills ('Section 1 of Chapter 534' for the SEC. 1.5. print)."""
    for b in ab557:
        assert "\n" not in (b.lineage or "")
    assert ab557[1].lineage == \
        "as amended by Section 1 of Chapter 285 of the Statutes of 2022"


def test_ab2302_heading_directly_after_colon(fixtures):
    """Flattened lobs do not reliably newline before headings; AB 2302's
    only enacting section rides the enacting clause's colon."""
    flat = _flat(fixtures, "20230AB230297CHP.lob")
    assert "follows:SECTION 1.Section 54953" in flat  # fixture guard
    blocks = section_blocks(flat, "Government Code", "54953")
    assert [b.heading for b in blocks] == ["SECTION 1."]
    assert blocks[0].body.startswith("(a)")


def test_body_strips_the_section_number_prefix(ab557):
    """Re-enacted text opens '54953. (a) All meetings …'; the number
    belongs to the citation, not the body."""
    for b in ab557[:3]:
        assert not b.body.startswith("54953")


def test_intro_is_flowed_prose(ab1754):
    intro = ab1754[1].intro
    assert intro == " ".join(intro.split())
    assert intro.startswith("SEC. 89.")
    assert intro.rstrip(":").endswith("to read")


# ------------------------------------------------------------- synthetic

_DEMO = ("An act to amend the Demo Code. The people of the State of "
         "California do enact as follows:"
         "SECTION 1.Section 5 of the Demo Code is amended to read:"
         "5. (a) Alpha text."
         "SEC. 2. Section 7 is added to the Demo Code, to read:"
         "7. (a) Beta text."
         "SEC. 3. Section 5.5 of the Demo Code is repealed.")


def test_split_blocks_prefix_plus_headings():
    parts = split_blocks(_DEMO)
    assert len(parts) == 4  # title/enacting-clause prefix + three blocks
    assert parts[1].startswith("SECTION 1.")
    assert parts[2].startswith("SEC. 2.")


def test_section_blocks_amended_form():
    (b,) = section_blocks(_DEMO, "Demo Code", "5")
    assert b.heading == "SECTION 1."
    assert b.action == "amended to read"
    assert b.lineage is None
    assert b.body == "(a) Alpha text."


def test_section_blocks_added_form():
    (b,) = section_blocks(_DEMO, "Demo Code", "7")
    assert b.action.startswith("added")
    assert b.body == "(a) Beta text."


def test_target_never_matches_a_decimal_sibling():
    """Searching § 5 must not hit the block repealing § 5.5 — and § 5.5
    must resolve to its own block."""
    assert len(section_blocks(_DEMO, "Demo Code", "5")) == 1
    (b,) = section_blocks(_DEMO, "Demo Code", "5.5")
    assert b.action == "repealed"
    assert b.body == ""


def test_further_amended_form():
    text = ("SEC. 4. Section 9 of the Demo Code, as amended by Section 2 "
            "of Chapter 100 of the Statutes of 2020, is further amended "
            "to read:9. (a) Gamma text.")
    (b,) = section_blocks(text, "Demo Code", "9")
    assert b.action == "amended to read"
    assert b.lineage == \
        "as amended by Section 2 of Chapter 100 of the Statutes of 2020"
    assert b.body == "(a) Gamma text."


def test_wrong_code_yields_no_blocks():
    assert section_blocks(_DEMO, "Government Code", "5") == []

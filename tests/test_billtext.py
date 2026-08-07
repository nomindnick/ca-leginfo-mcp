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
    """AB 557 prints SECTION 1. (no dot after SECTION), the contingent
    double-joint print SEC. 1.5. (never operative — SB 537 failed its
    condition), SEC. 2., and a repealed block."""
    assert [b.heading for b in ab557] == [
        "SECTION 1.", "SEC. 1.5.", "SEC. 2.", "SEC. 3."]
    assert [b.action for b in ab557][3] == "repealed"
    assert ab557[3].body == ""  # repealed blocks carry no body
    assert ab557[3].lineage == \
        "as added by Section 3 of Chapter 285 of the Statutes of 2022"


def test_lineage_is_whitespace_normalized(ab557):
    """The lob wraps 'Section 1\\nof Chapter 285' mid-parenthetical; the
    lineage key must come back flowed — later bills cite it literally in
    their intros, so the key must match without line-wrap noise."""
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


def test_intro_is_flowed_prose(ab557):
    """SEC. 1.5.'s raw intro line-wraps mid-lineage in the lob — the
    normalization must flow it, so this fixture discriminates (a raw
    slice would carry the newline)."""
    intro = ab557[1].intro
    assert "\n" not in intro
    assert intro.startswith("SEC. 1.5.")
    assert "as amended by Section 1 of Chapter 285" in intro
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


def test_glued_heading_amendment_still_splits():
    """A block amending a structural heading ends with the unterminated
    new title, gluing the next SEC. onto arbitrary text — the section it
    introduces must still be retrievable. (Real shape: AB 1762 (2003),
    whose Part-heading amendment swallowed SEC. 33 / Ins. Code
    § 12699.50; same class cost AB 731 (2015) its § 6254 amendment.)"""
    flat = ("The people of the State of California do enact as follows:"
            "SEC. 32.The heading of Part 6.4 (commencing with Section "
            "12699.50) of Division 2 of the Insurance Code is amended to "
            "read:6.4.COUNTY HEALTH INITIATIVE MATCHING FUND"
            "SEC. 33.Section 12699.50 of the Insurance Code is amended to "
            "read:12699.50.This part shall be known as the fund.")
    (b,) = section_blocks(flat, "Insurance Code", "12699.50")
    assert b.heading == "SEC. 33."
    assert b.body == "This part shall be known as the fund."


def test_lineage_comes_from_the_intro_not_the_body():
    """Lineage-shaped prose inside the re-enacted body (legislative
    findings love '…this section, as amended by this act, is…') must not
    be read as the intro's lineage parenthetical. (Real shape: AB 189
    (1991), Fish & Game Code § 8692.5.)"""
    flat = ("SEC. 14. Section 8692.5 of the Fish and Game Code is amended "
            "to read:8692.5. The Legislature finds that this section, as "
            "amended by this act, is more restrictive than federal law.")
    (b,) = section_blocks(flat, "Fish and Game Code", "8692.5")
    assert b.lineage is None


def test_trailing_corrections_apparatus_is_stripped():
    """Legislative Counsel's correction notice rides the last block's
    body (104 of 20,525 prints in the 2023-24 session) — it is print
    apparatus, not statute text, and would fabricate a redline change
    claiming the next amendment deleted it."""
    flat = ("SEC. 2. Section 5 of the Demo Code is amended to read:"
            "5. Alpha text remains the law.\nCORRECTIONS:\n"
            "Digest—Page 2.\n")
    (b,) = section_blocks(flat, "Demo Code", "5")
    assert b.body == "Alpha text remains the law."
    multi = ("SEC. 2. Section 5 of the Demo Code is amended to read:"
             "5. Alpha text.CORRECTIONS:Digest—Pages 2 and 3.\n"
             "Text—Page 5.")
    (b,) = section_blocks(multi, "Demo Code", "5")
    assert b.body == "Alpha text."


def test_corrections_like_prose_mid_body_survives():
    """Only the trailing apparatus shape is stripped — statute text that
    happens to mention corrections is content."""
    flat = ("SEC. 2. Section 5 of the Demo Code is amended to read:"
            "5. The department shall publish CORRECTIONS: a list of "
            "errata for each edition.")
    (b,) = section_blocks(flat, "Demo Code", "5")
    assert b.body.endswith("errata for each edition.")


def test_body_citation_of_another_section_is_not_a_match():
    """A block amending § Y whose re-enacted body opens by citing § X
    ("Notwithstanding Section X of the Government Code…") must not be
    returned as a version of § X — the target has to be cited in the
    intro sentence itself, before the action verb. (Real shape: AB 1222
    (2011) amends HSC 50904, whose body cites Gov. Code § 1090 ~240
    chars in, inside any plausible head window.)"""
    block = ("SECTION 1.Section 50904 of the Health and Safety Code is "
             "amended to read:50904.The representation of varied interest "
             "groups on the board is important. Notwithstanding Section "
             "1090 of the Government Code, a member shall not be deemed "
             "interested in a contract.")
    assert section_blocks(block, "Government Code", "1090") == []
    (b,) = section_blocks(block, "Health and Safety Code", "50904")
    assert b.body.startswith("The representation")

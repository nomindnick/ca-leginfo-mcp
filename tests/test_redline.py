"""Tests for server/redline.py — the subdivision-anchored redline engine.

The § 54953 version chain (tests/fixtures/sec54953/) pins the engine
end-to-end — bill lob → flattening → block extraction → redline — with
golden markdown under golden_redlines/. The chain covers a wholesale
rewrite (SB 707), sunset-branch endpoints, and a cross-source pair
(chaptered-bill text vs. current-law text) whose golden carries only
real changes: zero phantom hunks is the whitespace-insensitivity
contract (SPEC §10). Synthetic cases pin each engine rule.
"""

from pathlib import Path

import pytest

from ingest import caml
from server.billtext import section_blocks
from server.redline import _merge_ops, _pair, _segments, redline


def _blocks(fixtures: Path, name: str):
    return section_blocks(
        caml.bill_text(
            (fixtures / "sec54953" / name).read_text(encoding="utf-8")),
        "Government Code", "54953")


@pytest.fixture(scope="module")
def chain(fixtures):
    """§ 54953 version-chain endpoints, keyed as the goldens are named."""
    ab1754 = _blocks(fixtures, "20230AB175497CHP_excerpt.lob")
    ab557 = _blocks(fixtures, "20230AB55795CHP.lob")
    ab2302 = _blocks(fixtures, "20230AB230297CHP.lob")
    current = (fixtures / "sec54953" / "current_54953.txt").read_text()
    return {
        "AB1754s89__AB557s2": (ab1754[1].body, ab557[2].body),
        "AB557s1__AB2302s1": (ab557[0].body, ab2302[0].body),
        "AB557s2__current": (ab557[2].body, current.strip()),
        "repealed": (ab557[2].body, ab557[3].body),
    }


# ---------------------------------------------------------- golden chain

@pytest.mark.parametrize(("pair", "n_changes"), [
    ("AB1754s89__AB557s2", 46),
    ("AB557s1__AB2302s1", 6),
    ("AB557s2__current", 45),
])
def test_chain_matches_golden(fixtures, chain, pair, n_changes):
    old, new = chain[pair]
    r = redline(old, new)
    golden = (fixtures / "sec54953" / "golden_redlines" /
              f"{pair}.md").read_text()
    assert r.markdown == golden.rstrip("\n")
    assert len(r.changes) == n_changes
    assert not r.identical


def test_wholesale_rewrite_reads_like_an_attorney_wrote_it(fixtures):
    """SB 707's rewrite: relettering renders as `~~(c)~~ *(d)*` and a
    compound edit replaces the clause inline — not interleaved fragments,
    the failure mode that disqualified the flat word diff."""
    golden = (fixtures / "sec54953" / "golden_redlines" /
              "AB557s2__current.md").read_text()
    assert "~~(c)~~ *(d)*" in golden
    assert ("fringe benefits of ~~a local agency executive, as defined in "
            "subdivision (d) of Section 3511.1,~~ *either of the "
            "following*") in golden


def test_whole_provisions_insert(chain):
    """The operative edge AB 557 SECTION 1 → AB 2302: the teleconference
    caps arrive as whole new provisions. (The never-operative SEC. 1.5
    print is deliberately NOT a chain endpoint — redlining against it
    fabricates changes AB 2302 never made.)"""
    old, new = chain["AB557s1__AB2302s1"]
    r = redline(old, new)
    kinds = [c.kind for c in r.changes]
    assert kinds.count("new_provision") >= 3
    added = " ".join(c.added or "" for c in r.changes)
    assert "Two meetings per year" in added
    assert "Five meetings per year" in added


def test_whole_provisions_strike(chain):
    """SB 707 repealed the COVID-era teleconference subdivisions: they
    must strike out as whole provisions, not dissolve into word soup."""
    old, new = chain["AB557s2__current"]
    r = redline(old, new)
    deleted = [c.deleted for c in r.changes
               if c.kind == "deleted_provision"]
    assert len(deleted) >= 20
    assert any(d.startswith("(d) (1) Notwithstanding the provisions "
                            "relating to a quorum") for d in deleted)


def test_real_edit_context_is_pinned(chain):
    """The change list's context fields are load-bearing for tool output
    (they locate an edit inside a long section) — pin them on a real
    hunk, not just a synthetic one."""
    old, new = chain["AB557s2__current"]
    r = redline(old, new)
    (c,) = [c for c in r.changes
            if c.deleted and "3511.1" in c.deleted]
    assert c.kind == "edit"
    assert c.deleted == ("a local agency executive, as defined in "
                         "subdivision (d) of Section 3511.1,")
    assert c.added == "either of the following"
    assert c.context_before == "the form of fringe benefits of"
    assert c.context_after == "during the open meeting in which"


def test_repealed_block_comparison_has_no_phantom_change(chain):
    """Comparing live text against a repealed block's (correctly) empty
    body yields only whole-provision deletions — no empty-string change,
    no stray `**`/`~~~~` emphasis tokens in the markdown."""
    live, empty = chain["repealed"]
    assert empty == ""
    r = redline(live, empty)
    assert r.changes
    assert all(c.kind == "deleted_provision" for c in r.changes)
    assert all(c.deleted for c in r.changes)
    assert "**" not in r.markdown and "~~~~" not in r.markdown
    back = redline(empty, live)
    assert all(c.kind == "new_provision" and c.added for c in back.changes)
    assert redline("", "").identical


def test_unicode_typography_folds_never_redline_dirty():
    """Mid-2000s bill lobs print non-breaking hyphens (U+2011) and curly
    quotes where law lobs print ASCII — character-identical text must
    compare identical, not emit `~~full‑time~~ *full-time*`. (Real
    shape: BPC 1640.3 vs its chapter's lob, AB 1143 (2005).)"""
    bill = "(a) A person approved as a full‑time professor of " \
           "“dentistry” at the board’s discretion."
    law = "(a) A person approved as a full-time professor of " \
          "\"dentistry\" at the board's discretion."
    r = redline(bill, law)
    assert r.identical
    assert r.changes == []


def test_glued_subdivision_marker_segments_like_spaced():
    """Flattened bill lobs sometimes glue a subdivision marker to the
    preceding sentence ("…in Sudan.(2) Investments…") where law lobs
    space it — the sources must still compare identical."""
    glued = ("(1) Investments in a company primarily engaged in supplying "
             "goods intended to relieve human suffering in Sudan."
             "(2) Investments in a bank making loans there.")
    spaced = glued.replace("Sudan.(2)", "Sudan. (2)")
    r = redline(glued, spaced)
    assert r.identical
    assert r.changes == []


def test_marker_glued_to_following_word_is_not_an_edit():
    """The complementary glue: bill lobs print '(ib)This' where law lobs
    print '(ib) This' — word-identical text must compare identical, not
    fabricate a phantom provision. (Real shape: RTC 214, Stats. 2023,
    Ch. 734 vs current law; four more sections confirmed in round 1.)"""
    bill = "size. (ib)This subclause shall only be operative."
    law = "size. (ib) This subclause shall only be operative."
    r = redline(bill, law)
    assert r.identical
    assert r.changes == []
    # De-gluing happens only at segment positions: an inline citation's
    # markers are untouched and never fabricate a change either.
    cite = "pursuant to subdivision (a)(1) of Section 3, the clerk acts."
    assert redline(cite, cite).identical
    assert _segments(cite) == [cite]


def test_unamended_prints_are_affirmatively_identical(fixtures):
    """AB 2302 passed without amendment: its introduced and chaptered
    § 54953 blocks must compare as identical — the tools assert sameness,
    never infer it from an empty diff (SPEC §12)."""
    int_b = _blocks(fixtures, "20230AB230299INT.lob")
    chp_b = _blocks(fixtures, "20230AB230297CHP.lob")
    r = redline(int_b[0].body, chp_b[0].body)
    assert r.identical
    assert r.changes == []
    assert "~~" not in r.markdown


# ------------------------------------------------------------- synthetic

def test_layout_differences_are_never_edits():
    """Whitespace-insensitive: the same words in different line-wrapping
    and indentation yield an identical result, not phantom hunks."""
    flowed = "(a) All meetings shall be open. (b) The public may attend."
    wrapped = "(a) All meetings\n   shall be open.\n(b) The public\nmay attend."
    r = redline(wrapped, flowed)
    assert r.identical
    assert r.markdown == "(a) All meetings shall be open.\n" \
                         "(b) The public may attend."


def test_word_edit_carries_context():
    r = redline(
        "The clerk shall post the agenda at least 72 hours before the "
        "meeting.",
        "The clerk shall post the agenda at least 102 hours before the "
        "meeting.")
    (c,) = r.changes
    assert c.kind == "edit"
    assert (c.deleted, c.added) == ("72", "102")
    assert c.context_before == "shall post the agenda at least"
    assert c.context_after == "hours before the meeting."
    assert "~~72~~ *102*" in r.markdown


def test_new_provision_is_a_whole_segment():
    r = redline("(a) Alpha remains. (b) Beta remains.",
                "(a) Alpha remains. (b) Beta remains. (c) Gamma arrives.")
    (c,) = r.changes
    assert c.kind == "new_provision"
    assert c.added == "(c) Gamma arrives."
    assert r.markdown.endswith("*(c) Gamma arrives.*")


def test_segments_split_only_after_sentence_punctuation():
    assert _segments("(a) One. (b) Two. (c) Three.") == \
        ["(a) One.", "(b) Two.", "(c) Three."]
    # A mid-sentence citation's "(b)" is not a subdivision start.
    assert _segments(
        "pursuant to subdivision (b) of Section 3, the clerk acts.") == \
        ["pursuant to subdivision (b) of Section 3, the clerk acts."]


def test_pair_floor_rejects_unrelated_provisions():
    """Below the 0.5 ratio floor, a replaced segment is a delete plus an
    insert — word-diffing unrelated provisions produces interleaved
    nonsense."""
    old = "(b) The auditor shall certify the claim within 30 days."
    new = "(b) Members may attend by teleconference from a publicized " \
          "location."
    assert _pair([old], [new]) == [(0, None), (None, 0)]
    r = redline(f"(a) Same intro. {old}", f"(a) Same intro. {new}")
    assert [c.kind for c in r.changes] == \
        ["deleted_provision", "new_provision"]
    assert f"~~{old}~~" in r.markdown
    assert f"*{new}*" in r.markdown


def test_merge_ops_absorbs_short_equal_runs():
    """An equal run of ≤2 tokens between changes is absorbed into one
    replace hunk (the matcher anchoring on a stray 'of'); a 3-token run
    is a real anchor and survives."""
    ops = [("replace", 0, 2, 0, 2), ("equal", 2, 4, 2, 4),
           ("delete", 4, 6, 4, 4)]
    assert _merge_ops(ops) == [("replace", 0, 6, 0, 4)]
    ops3 = [("replace", 0, 2, 0, 2), ("equal", 2, 5, 2, 5),
            ("delete", 5, 7, 5, 5)]
    assert _merge_ops(ops3) == ops3

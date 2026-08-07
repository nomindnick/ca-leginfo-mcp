"""Tests for server/texttools.py — tools 8–10 (SPEC §12) against
fixture-built databases.

The compare bed grafts the real § 54953 chapter chain
(tests/fixtures/sec54953/, see its README) onto the builder-produced
mini current.db and 1989+1999 archive: AB 557 / AB 2302 / AB 1754 become
archive bills with their fixture prints as stored version text, and
current law gets § 54953 as it stood after AB 2302 — so the
history-note chain walk, the double-joint variant trap (SEC. 1.5 must
never be picked), and the golden redline are all exercised through the
tool surface, not just the engine. Mini-native bills (AB 831's six
prints, AB 1656's pending PEN 1050 amendment) cover the current-session
paths on wholly builder-produced data.
"""

import sqlite3
import zlib
from pathlib import Path

import pytest

from ingest import caml
from ingest.archive import build_archive_db
from ingest.build import build_current_db
from server import texttools
from server.billtext import section_blocks
from server.db import Databases

FIXTURES = Path(__file__).parent / "fixtures"

_54953_NOTE = ("Amended (as amended by Stats. 2023, Ch. 534, Sec. 1) by "
               "Stats. 2024, Ch. 389, Sec. 1.   (AB 2302)   Effective "
               "January 1, 2025.")


def _xml(name: str) -> str:
    return (FIXTURES / "sec54953" / name).read_text(encoding="utf-8")


def _inject_chain(current_db: Path, archive_db: Path) -> None:
    """Graft the § 54953 chapter chain onto the fixture stores."""
    arc = sqlite3.connect(archive_db)
    for bid, num, cy, cn, latest in [
            ("202320240AB557", "557", "2023", "534", "20230AB55795CHP"),
            ("202320240AB2302", "2302", "2024", "389", "20230AB230297CHP"),
            ("202320240AB1754", "1754", "2023", "131", "20230AB175497CHP")]:
        arc.execute(
            """INSERT INTO bill(bill_id, session_year, session_num,
                   measure_type, measure_num, chapter_year, chapter_type,
                   chapter_num, chapter_session_num, current_status,
                   latest_bill_version_id)
               VALUES (?, '20232024', '0', 'AB', ?, ?, 'CHP', ?, '0',
                       'Chaptered', ?)""", (bid, num, cy, cn, latest))
    for vid, bid, vn, act, date, lob in [
            ("20230AB55795CHP", "202320240AB557", "95", "Chaptered",
             "2023-10-13 00:00:00", "20230AB55795CHP.lob"),
            ("20230AB230297CHP", "202320240AB2302", "97", "Chaptered",
             "2024-09-22 00:00:00", "20230AB230297CHP.lob"),
            ("20230AB230299INT", "202320240AB2302", "99", "Introduced",
             "2024-02-12 00:00:00", "20230AB230299INT.lob"),
            ("20230AB175497CHP", "202320240AB1754", "97", "Chaptered",
             "2023-07-27 00:00:00", "20230AB175497CHP_excerpt.lob")]:
        arc.execute(
            """INSERT INTO bill_version(bill_version_id, bill_id,
                   version_num, action, action_date) VALUES (?,?,?,?,?)""",
            (vid, bid, vn, act, date))
        xml = _xml(lob)
        arc.execute(
            "INSERT INTO bill_version_text VALUES (?,?,?)",
            (vid, caml.extract_title(xml),
             zlib.compress(caml.bill_text(xml).encode())))
    # An introduced print listed in the dat but with no published lob —
    # the shape behind "no text is stored for this version".
    arc.execute(
        """INSERT INTO bill_version(bill_version_id, bill_id, version_num,
               action, action_date)
           VALUES ('20230AB55799INT', '202320240AB557', '99',
                   'Introduced', '2023-02-08')""")
    # Same shape in the chaptered-only era, for the pre-1999 coverage
    # statement (SB 729 of 1989 stores only its chaptered print).
    arc.execute(
        """INSERT INTO bill_version(bill_version_id, bill_id, version_num,
               action, action_date)
           VALUES ('SB72999INT', '198919900SB729', '99', 'Introduced',
                   '1989-03-01')""")
    # Chapters whose bills' titles cite BPC § 17539.1 — the title-based
    # citing-bills fallback's candidates. The two texted ones reuse
    # AB 831's real chaptered text (it carries the § 17539.1 block); the
    # 2020 pair exists to pin CHAPTERED-DATE ordering: the 1st Ex. Sess.
    # Ch. 3 (Nov 2020) is months newer than regular Ch. 800 (Mar 2020)
    # despite its tiny chapter number (the round-1 WIC 13600 shape).
    flat831 = caml.bill_text(
        (FIXTURES / "mini" / "BILL_VERSION_TBL_9506.lob")
        .read_text(encoding="utf-8"))
    citing = [
        # (bill_id, num, sess, ch_year, ch_num, ch_sess, vid, date, text?)
        ("201920200AB100", "100", "20192020", "2019", "100", "0",
         "20190AB10095CHP", "2019-09-01 00:00:00", True),
        ("202120220AB50", "50", "20212022", "2021", "50", "0",
         "20210AB5095CHP", "2021-09-01 00:00:00", True),
        ("201920200AB900", "900", "20192020", "2020", "800", "0",
         "20190AB90095CHP", "2020-03-01 00:00:00", False),
        ("201920201AB5", "5", "20192020", "2020", "3", "1",
         "20191AB595CHP", "2020-11-01 00:00:00", False),
    ]
    for bid, num, sess, cy, cn, cs, vid, date, texted in citing:
        arc.execute(
            """INSERT INTO bill(bill_id, session_year, session_num,
                   measure_type, measure_num, chapter_year, chapter_type,
                   chapter_num, chapter_session_num, current_status,
                   latest_bill_version_id)
               VALUES (?, ?, ?, 'AB', ?, ?, 'CHP', ?, ?, 'Chaptered',
                       ?)""", (bid, sess, cs, num, cy, cn, cs, vid))
        arc.execute(
            """INSERT INTO bill_version(bill_version_id, bill_id,
                   version_num, action, action_date)
               VALUES (?, ?, '95', 'Chaptered', ?)""", (vid, bid, date))
        if texted:
            arc.execute(
                "INSERT INTO bill_version_text VALUES (?, NULL, ?)",
                (vid, zlib.compress(flat831.encode())))
        arc.execute(
            """INSERT INTO bill_section_ref(session_year, bill_version_id,
                   bill_id, action, law_code, section, is_range)
               VALUES (?, ?, ?, 'amend', 'BPC', '17539.1', 0)""",
            (sess[:4], vid, bid))
    # A repeal-and-add chain for GOV § 99999 (not in the mini law
    # slice): Stats. 2018, Ch. 55 prints two parallel variants; Stats.
    # 2020, Ch. 77 repeals (its repealed block's lineage citing the
    # SEC. 4 variant) and re-adds the section.
    ch55 = ("An act to amend Section 99999 of the Government Code. "
            "The people of the State of California do enact as follows:"
            "SECTION 1.Section 99998 of the Government Code is amended "
            "to read:99998. Unrelated text."
            "SEC. 3.Section 99999 of the Government Code is amended to "
            "read:99999. Old text A of the section. (a) Alpha."
            "SEC. 4.Section 99999 of the Government Code is amended to "
            "read:99999. Old text B of the section. (a) Alpha.")
    ch77 = ("An act to repeal and add Section 99999 of the Government "
            "Code. The people of the State of California do enact as "
            "follows:"
            "SECTION 1.Section 99999 of the Government Code, as amended "
            "by Section 4 of Chapter 55 of the Statutes of 2018, is "
            "repealed."
            "SEC. 2.Section 99999 is added to the Government Code, to "
            "read:99999. New text of the section. (a) Alpha.")
    for bid, num, sess, cy, cn, vid, date, flat in [
            ("201720180AB55", "55", "20172018", "2018", "55",
             "20170AB5595CHP", "2018-08-01 00:00:00", ch55),
            ("201920200AB77", "77", "20192020", "2020", "77",
             "20190AB7795CHP", "2020-09-25 00:00:00", ch77)]:
        arc.execute(
            """INSERT INTO bill(bill_id, session_year, session_num,
                   measure_type, measure_num, chapter_year, chapter_type,
                   chapter_num, chapter_session_num, current_status,
                   latest_bill_version_id)
               VALUES (?, ?, '0', 'AB', ?, ?, 'CHP', ?, '0', 'Chaptered',
                       ?)""", (bid, sess, num, cy, cn, vid))
        arc.execute(
            """INSERT INTO bill_version(bill_version_id, bill_id,
                   version_num, action, action_date)
               VALUES (?, ?, '95', 'Chaptered', ?)""", (vid, bid, date))
        arc.execute(
            "INSERT INTO bill_version_text VALUES (?, NULL, ?)",
            (vid, zlib.compress(flat.encode())))
        arc.execute(
            """INSERT INTO bill_section_ref(session_year, bill_version_id,
                   bill_id, action, law_code, section, is_range)
               VALUES (?, ?, ?, 'amend', 'GOV', '99999', 0)""",
            (sess[:4], vid, bid))
    arc.commit()
    arc.close()

    cur = sqlite3.connect(current_db)
    ab2302 = section_blocks(caml.bill_text(_xml("20230AB230297CHP.lob")),
                            "Government Code", "54953")[0].body
    assert not cur.execute("SELECT 1 FROM law_section WHERE law_code='GOV'"
                           " AND section_num_norm='54953'").fetchone()
    cur.execute(
        """INSERT INTO law_section(law_code, section_num, section_num_norm,
               history, content_text, active_flg)
           VALUES ('GOV', '54953.', '54953', ?, ?, 'Y')""",
        (_54953_NOTE, ab2302))
    # PEN 337o: added (not amended) by mini's own AB 831 — the
    # no-prior-version path rides a wholly real current-session bill.
    (blob,) = cur.execute(
        """SELECT text_zlib FROM bill_version_text
           WHERE bill_version_id='20250AB83194CHP'""").fetchone()
    b337 = section_blocks(zlib.decompress(blob).decode(), "Penal Code",
                          "337o")
    assert b337 and b337[0].action.startswith("added")
    cur.execute(
        """INSERT INTO law_section(law_code, section_num, section_num_norm,
               history, content_text, active_flg)
           VALUES ('PEN', '337o.', '337o',
                   'Added by Stats. 2025, Ch. 623, Sec. 2.   (AB 831)',
                   ?, 'Y')""", (b337[0].body,))
    # BPC 17539.1 (already in the mini law slice, amended by AB 831):
    # rewrite its history to the ordinary no-parenthetical form so the
    # zero-argument default must ride the citing-bills fallback.
    n = cur.execute(
        """UPDATE law_section
           SET history='Amended by Stats. 2025, Ch. 623, Sec. 2.   '
                       || '(AB 831)   Effective January 1, 2026.'
           WHERE law_code='BPC' AND section_num_norm='17539.1'""").rowcount
    assert n == 1
    # GOV 99999: the repeal-and-add story (see the archive chain above).
    cur.execute(
        """INSERT INTO law_section(law_code, section_num, section_num_norm,
               history, content_text, active_flg)
           VALUES ('GOV', '99999.', '99999',
                   'Repealed and added by Stats. 2020, Ch. 77, Sec. 2.   '
                   || '(AB 77)   Effective January 1, 2021.',
                   'New text of the section. (a) Alpha.', 'Y')""")
    # PEN 337p: an "Added by renumbering" note — must NOT claim
    # no-prior-version (renumbered sections have priors under their old
    # numbers).
    cur.execute(
        """INSERT INTO law_section(law_code, section_num, section_num_norm,
               history, content_text, active_flg)
           VALUES ('PEN', '337p.', '337p',
                   'Added by renumbering of Section 337o by Stats. 2025, '
                   || 'Ch. 623, Sec. 2.   (AB 831)', 'Renumbered text.',
                   'Y')""")
    # A second simultaneous GOV § 54953 row (the future-operative sunset
    # branch, as the real store carries) — inserted AFTER the original
    # so rowid order keeps the original as the deterministic pick.
    ab557s2 = section_blocks(caml.bill_text(_xml("20230AB55795CHP.lob")),
                             "Government Code", "54953")[2].body
    cur.execute(
        """INSERT INTO law_section(law_code, section_num, section_num_norm,
               history, content_text, active_flg)
           VALUES ('GOV', '54953.', '54953',
                   'Amended (as amended by Stats. 2023, Ch. 534, Sec. 2) '
                   || 'by Stats. 2024, Ch. 389, Sec. 1.   (AB 2302)   '
                   || 'Operative January 1, 2026.', ?, 'Y')""", (ab557s2,))
    cur.commit()
    cur.close()


@pytest.fixture(scope="module")
def dbs(mini_zip, archive_zips_dir, tmp_path_factory) -> Databases:
    d = tmp_path_factory.mktemp("texttools_dbs")
    build_current_db(mini_zip, d / "current.db")
    build_archive_db(
        sorted(archive_zips_dir.glob("pubinfo_*.zip")), d / "archive.db")
    _inject_chain(d / "current.db", d / "archive.db")
    return Databases(d / "current.db", d / "archive.db")


@pytest.fixture(scope="module")
def v1_dbs(dbs, tmp_path_factory) -> Databases:
    """A current.db as the deployed v1 artifact looks the morning after a
    code deploy but before the nightly rebuild: no bill_version_text."""
    d = tmp_path_factory.mktemp("v1_shape")
    v1 = d / "current.db"
    v1.write_bytes(dbs.current_path.read_bytes())
    con = sqlite3.connect(v1)
    con.execute("DROP TABLE bill_version_text")
    con.commit()
    con.close()
    return Databases(v1, None)


def assert_envelope(r):
    assert r["law_extract_date"]
    assert r["bill_extract_date"]
    assert "bulk-data downloads" in r["source"]
    assert r["current_session"] == "2025-2026"


# =========================================================================
# tool 8: get_bill_text
# =========================================================================

def test_full_text_of_a_small_print(dbs):
    r = texttools.get_bill_text(dbs, "AB 831")
    assert_envelope(r)
    assert r["measure"] == "AB 831"
    assert r["version"]["action"] == "Chaptered"  # default = latest
    assert "The people of the State of California do enact" in r["text"]
    assert r["title"].startswith("An act to amend Section 17539.1")
    assert len(r["other_versions"]) == 5
    assert "sections_index" not in r


def test_version_argument_forms(dbs):
    by_action = texttools.get_bill_text(dbs, "AB 831",
                                        version="introduced")
    assert by_action["version"]["version_num"] == "99"
    by_num = texttools.get_bill_text(dbs, "AB 831", version="99")
    assert by_num["version"]["version_id"] == \
        by_action["version"]["version_id"]
    by_id = texttools.get_bill_text(dbs, "AB 831",
                                    version="20250AB83194CHP")
    assert by_id["version"]["action"] == "Chaptered"


def test_ambiguous_version_phrase_warns(dbs):
    r = texttools.get_bill_text(dbs, "AB 831", version="amended")
    assert "text" in r
    assert any("prints match 'amended'" in n for n in r["notes"])


def test_unknown_version_lists_available(dbs):
    r = texttools.get_bill_text(dbs, "AB 831", version="42")
    assert "error" in r
    assert len(r["available_versions"]) == 6


def test_oversize_print_returns_sec_index(dbs):
    """AB 1754's omnibus print (59k chars) crosses MAX_FULL_TEXT: the
    response is a navigable index, not text (decision D3)."""
    r = texttools.get_bill_text(dbs, "AB 1754", session="2023-2024")
    assert "text" not in r
    assert r["text_chars"] > texttools.MAX_FULL_TEXT
    idx = r["sections_index"]
    assert [e["heading"] for e in idx[:2]] == ["SECTION 1.", "SEC. 88."]
    sec89 = next(e for e in idx if e["heading"] == "SEC. 89.")
    assert "Section 54953 of the Government Code" in sec89["intro"]
    assert "as amended by Section 2 of Chapter 285" in sec89["intro"]
    assert all(len(e["intro"]) <= 200 for e in idx)
    assert any("section_filter" in n for n in r["notes"])


def test_section_filter_returns_variant_blocks(dbs):
    r = texttools.get_bill_text(dbs, "AB 1754", session="2023-2024",
                                section_filter="GOV 54953")
    assert [b["heading"] for b in r["blocks"]] == \
        ["SEC. 88.", "SEC. 89.", "SEC. 90."]
    assert all(b["text"].startswith("(a)") for b in r["blocks"])
    assert r["blocks"][1]["lineage"] == \
        "as amended by Section 2 of Chapter 285 of the Statutes of 2022"
    assert any("3 blocks" in n for n in r["notes"])


def test_section_filter_citation_order_form(dbs):
    r = texttools.get_bill_text(
        dbs, "AB 1754", session="2023-2024",
        section_filter="Section 54953 of the Government Code")
    assert len(r["blocks"]) == 3


def test_section_filter_miss_falls_back_to_index(dbs):
    r = texttools.get_bill_text(dbs, "AB 2302", session="2023-2024",
                                section_filter="PEN 187")
    assert r["blocks"] == []
    assert r["sections_index"]
    assert any("No enacting section" in n for n in r["notes"])


def test_section_filter_parse_error(dbs):
    r = texttools.get_bill_text(dbs, "AB 831", section_filter="gibberish")
    assert "error" in r and "expected_format" in r


def test_untexted_version_is_explained(dbs):
    r = texttools.get_bill_text(dbs, "AB 557", session="2023-2024",
                                version="introduced")
    assert "No text is stored" in r["error"]
    assert r["available_versions"] == \
        ["Chaptered (2023-10-13, version 95)"]


def test_v1_artifact_degrades_honestly(v1_dbs):
    r = texttools.get_bill_text(v1_dbs, "AB 831")
    assert "nightly" in r["error"]


# =========================================================================
# tool 9: compare_section_versions
# =========================================================================

def test_zero_arg_default_matches_golden(dbs, fixtures):
    """The zero-argument redline: prior operative version → current.
    The history note's parenthetical names Stats. 2023, Ch. 534, Sec. 1 —
    so AB 557's SECTION 1. must be picked (never the SEC. 1.5
    double-joint print), and the output must be byte-identical to the
    engine's golden for that edge."""
    r = texttools.compare_section_versions(dbs, "Gov. Code", "54953")
    assert_envelope(r)
    f, t = r["from"], r["to"]
    assert f["citation"] == "Stats. 2023, Ch. 534"
    assert f["measure"] == "AB 557"
    assert f["block"] == "SECTION 1."
    assert f["lineage"] == \
        "as amended by Section 1 of Chapter 285 of the Statutes of 2022"
    assert t["citation"] == "current law"
    assert t["history_note"] == _54953_NOTE
    assert not r["identical"]
    golden = (fixtures / "sec54953" / "golden_redlines" /
              "AB557s1__AB2302s1.md").read_text()
    assert r["redline_markdown"] == golden.rstrip("\n")
    assert len(r["changes"]) == 6
    assert r["notes"][0] == texttools.VERBATIM_NOTE
    # The unpicked variants are listed with their lineage — loud, never
    # silent (SPEC §12).
    variant_note = next(n for n in r["notes"] if "SEC. 1.5." in n)
    assert "SEC. 2." in variant_note and "SEC. 3." in variant_note
    assert "act section 1" in variant_note


def test_explicit_chapter_refs_hit_the_same_endpoints(dbs):
    r = texttools.compare_section_versions(
        dbs, "GOV", "54953", from_ref="Stats. 2023, Ch. 534",
        to_ref="current")
    assert r["from"]["block"] == "SECTION 1."
    r2 = texttools.compare_section_versions(
        dbs, "GOV", "54953",
        from_ref="Chapter 534 of the Statutes of 2023")
    assert r2["from"]["version_id"] == r["from"]["version_id"]
    assert r2["redline_markdown"] == r["redline_markdown"]


def test_identical_endpoints_affirmed(dbs):
    """Stats. 2024, Ch. 389 re-enacted the text current law carries —
    the tool must say 'no textual change', not hand back an empty diff."""
    r = texttools.compare_section_versions(
        dbs, "GOV", "54953", from_ref="Stats. 2024, Ch. 389")
    assert r["identical"] is True
    assert "No textual change" in r["statement"]
    assert "Stats. 2024, Ch. 389" in r["statement"]
    assert "redline_markdown" not in r


def test_pending_measure_ref_is_impact_analysis(dbs):
    """D2: to_ref='AB 1656' redlines current law against the pending
    print's proposed text, and says the text is proposed, not law."""
    r = texttools.compare_section_versions(dbs, "Pen. Code", "1050",
                                           to_ref="AB 1656")
    assert r["from"]["citation"] == "current law"
    assert r["to"]["measure"] == "AB 1656"
    assert "version" in r["to"]["citation"]
    assert r["to"]["source"] == "pending print"
    assert not r["identical"]
    assert any("proposed, not law" in n for n in r["notes"])


def test_default_rides_citing_bills_fallback(dbs):
    """The ordinary case: no parenthetical in the note, no lineage in
    the print. The prior version must come from enacted bills citing
    the section (title-based, SPEC §12's third resolution leg), picking
    the NEWEST chapter before the operative one — and the identical
    re-enactment is affirmed, not left as an empty diff."""
    r = texttools.compare_section_versions(dbs, "B&P Code", "17539.1")
    assert r["from"]["citation"] == "Stats. 2021, Ch. 50"
    assert r["from"]["block"] == "SEC. 2."
    assert any("title-based lineage" in n for n in r["notes"])
    assert r["identical"] is True
    assert "No textual change" in r["statement"]


def test_prior_citing_chapter_orders_by_chaptered_date(dbs):
    """The fallback walks the citing chain by CHAPTERED DATE:
    1st Ex. Sess. Ch. 3 (Nov 2020) must beat regular Ch. 800 (Mar 2020)
    despite its tiny chapter number — the round-1 WIC 13600 defect."""
    with dbs.current() as con:
        ladder = [
            ((2025, 623, 0), (2021, 50, 0)),
            ((2021, 50, 0), (2020, 3, 1)),   # ex-session wins on date
            ((2020, 3, 1), (2020, 800, 0)),
            ((2020, 800, 0), (2019, 100, 0)),
        ]
        for before, expect in ladder:
            got = texttools._prior_citing_chapter(
                dbs, con, "BPC", "17539.1", before=before)
            assert (got["year"], got["chapter"], got["ex"]) == expect, \
                (before, got)
        assert texttools._prior_citing_chapter(
            dbs, con, "BPC", "17539.1", before=(2019, 100, 0)) is None


def test_repealed_and_added_uses_pre_repeal_text(dbs):
    """A 'Repealed and added' note must never claim no-prior-version:
    the repealing block's own lineage names the repealed variant, so the
    zero-arg default compares the pre-repeal SEC. 4 text (round-1
    finding: 5,146 such notes got a false affirmative)."""
    r = texttools.compare_section_versions(dbs, "GOV", "99999")
    assert "no_prior_version" not in r
    assert r["from"]["citation"] == "Stats. 2018, Ch. 55"
    assert r["from"]["block"] == "SEC. 4."  # the repealed lineage's pick
    assert r["to"]["citation"] == "current law"
    assert not r["identical"]
    deleted = " ".join(c.get("deleted") or "" for c in r["changes"])
    added = " ".join(c.get("added") or "" for c in r["changes"])
    assert "Old text B" in deleted and "New text" in added
    assert any("repealed and re-added" in n for n in r["notes"])


def test_added_by_renumbering_is_not_no_prior(dbs):
    r = texttools.compare_section_versions(dbs, "PEN", "337p")
    assert "no_prior_version" not in r
    assert "individually extractable" in r["error"]


def test_simultaneous_current_rows_pick_is_loud(dbs):
    r = texttools.compare_section_versions(dbs, "Gov. Code", "54953")
    note = next(n for n in r["notes"] if "simultaneous versions" in n)
    assert "2 simultaneous versions" in note
    assert "Pass a chapter citation" in note
    assert "Operative January 1, 2026" in note  # the unpicked branch


def test_no_hint_multi_variant_pick_is_first_and_loud(dbs):
    """Stats. 2023, Ch. 131 (AB 1754) is off the history-note chain, so
    no act-section hint reaches it: the first printed block must be
    compared and the note must say the operative variant could not be
    established."""
    r = texttools.compare_section_versions(
        dbs, "GOV", "54953", from_ref="Stats. 2023, Ch. 131",
        to_ref="current")
    assert r["from"]["block"] == "SEC. 88."
    note = next(n for n in r["notes"] if "Stats. 2023, Ch. 131" in n
                and "blocks" in n)
    assert "could not be established" in note
    assert "SEC. 89." in note and "SEC. 90." in note


def test_walk_hints_teaches_from_lineage(dbs):
    """_walk_hints must actually walk: starting at the operative chapter
    it reads AB 2302's lineage and teaches the hint for Ch. 534."""
    hints = {(2024, 389, 0): "1"}
    with dbs.current() as con:
        texttools._walk_hints(
            dbs, con, "GOV", "54953",
            {"year": 2024, "chapter": 389, "ex": 0, "hint": "1"},
            hints, {(2023, 534, 0)})
    assert hints[(2023, 534, 0)] == "1"


def test_added_section_has_no_prior_version(dbs):
    r = texttools.compare_section_versions(dbs, "PEN", "337o")
    assert r["no_prior_version"] is True
    assert "was added by Stats. 2025, Ch. 623 (AB 831)" in r["statement"]


def test_pre_1989_ref_is_marked(dbs):
    r = texttools.compare_section_versions(
        dbs, "GOV", "54953", from_ref="Stats. 1984, Ch. 161")
    assert r["resolution"] == "predates_electronic_records"
    assert "1989" in r["error"]


def test_unknown_chapter_ref(dbs):
    r = texttools.compare_section_versions(
        dbs, "GOV", "54953", from_ref="Stats. 2023, Ch. 9999")
    assert "did not match any bill" in r["error"]


def test_unparseable_ref(dbs):
    r = texttools.compare_section_versions(dbs, "GOV", "54953",
                                           from_ref="the vibes")
    assert "error" in r and "expected_format" in r


def test_constitution_is_explicitly_unsupported(dbs):
    r = texttools.compare_section_versions(dbs, "Cal. Const.",
                                           "Art. I, Sec. 3")
    assert "resolution chapters" in r["error"]


def test_unknown_section_gets_suggestions(dbs):
    r = texttools.compare_section_versions(dbs, "GOV", "54953.99")
    assert "error" in r and "suggestions" in r


# =========================================================================
# tool 10: compare_bill_versions
# =========================================================================

def test_default_latest_vs_predecessor(dbs):
    r = texttools.compare_bill_versions(dbs, "AB 831")
    assert_envelope(r)
    assert r["from"]["action"] == "Enrolled"
    assert r["to"]["action"] == "Chaptered"
    assert isinstance(r["identical"], bool)
    assert r["title_and_digest"]["identical"] in (True, False)
    keys = list(r)
    assert keys.index("title_and_digest") < keys.index("body")


def test_unamended_prints_affirmed_identical(dbs):
    """AB 2302 passed without amendment; its introduced and chaptered
    prints are byte-identical in the archive."""
    r = texttools.compare_bill_versions(
        dbs, "AB 2302", session="2023-2024",
        from_version="introduced", to_version="chaptered")
    assert r["identical"] is True
    assert "No textual change" in r["statement"]
    assert r["title_and_digest"]["identical"] is True
    assert r["body"]["identical"] is True


def test_introduced_vs_chaptered_digest_first(dbs):
    """1999's AB 1 (five real prints): the digest redline is its own
    part, before the body (SPEC §12: Legislative Counsel's summary of
    the change is signal)."""
    r = texttools.compare_bill_versions(
        dbs, "AB 1", session="1999-2000",
        from_version="introduced", to_version="chaptered")
    assert not r["identical"]
    head, body = r["title_and_digest"], r["body"]
    assert not head["identical"] or not body["identical"]
    for part in (head, body):
        if not part["identical"]:
            assert part["redline_markdown"]
            assert part["changes"]


def test_single_print_bill_has_no_predecessor(dbs):
    r = texttools.compare_bill_versions(dbs, "SJR 31",
                                        session="1989-1990")
    assert "no print before" in r["error"]
    assert any("chaptered" in c for c in r["coverage"])


def test_default_hits_untexted_predecessor_coverage(dbs):
    """SB 729's dat lists an introduced print with no published lob —
    the zero-argument compare walks into it and must surface the
    chaptered-only coverage statement, not a bare miss."""
    r = texttools.compare_bill_versions(dbs, "SB 729",
                                        session="1989-1990")
    assert "No text is stored" in r["error"]
    assert any("chaptered-only" in c for c in r["coverage"])


def test_pre_1999_untexted_print_coverage_statement(dbs):
    r = texttools.compare_bill_versions(
        dbs, "SB 729", session="1989-1990",
        from_version="introduced", to_version="chaptered")
    assert "No text is stored" in r["error"]
    assert any("chaptered-only" in c for c in r["coverage"])


def test_untexted_modern_print(dbs):
    r = texttools.compare_bill_versions(
        dbs, "AB 557", session="2023-2024", from_version="introduced")
    assert "No text is stored" in r["error"]
    assert "coverage" not in r


def test_swapped_direction_notes(dbs):
    r = texttools.compare_bill_versions(dbs, "AB 831",
                                        from_version="chaptered",
                                        to_version="enrolled")
    assert any("back-to-front" in n for n in r["notes"])


def test_v1_artifact_degrades_honestly_compare(v1_dbs):
    r = texttools.compare_bill_versions(v1_dbs, "AB 831")
    assert "nightly" in r["error"]


def test_digest_split_is_real_not_vestigial(dbs):
    """AB 831's title/digest changed between introduction and
    chaptering: the title_and_digest part must carry its own redline —
    if the enacting-clause split silently stopped working, both prints
    would land in 'body' and this part would claim not-applicable."""
    r = texttools.compare_bill_versions(dbs, "AB 831",
                                        from_version="introduced",
                                        to_version="chaptered")
    head = r["title_and_digest"]
    assert head["identical"] is False
    assert head["changes"]
    assert "Not applicable" not in head.get("statement", "")


def test_date_version_argument(dbs):
    intro = texttools.get_bill_text(dbs, "AB 831", version="99")
    date = intro["version"]["date"][:10]
    by_date = texttools.get_bill_text(dbs, "AB 831", version=date)
    assert by_date["version"]["version_num"] == "99"


def test_corrupt_blob_is_an_error_not_a_crash(dbs, tmp_path):
    corrupt = tmp_path / "current.db"
    corrupt.write_bytes(dbs.current_path.read_bytes())
    con = sqlite3.connect(corrupt)
    con.execute("""UPDATE bill_version_text SET text_zlib=X'DEADBEEF'
                   WHERE bill_version_id='20250AB83199INT'""")
    con.commit()
    con.close()
    cdbs = Databases(corrupt, None)
    r = texttools.get_bill_text(cdbs, "AB 831", version="introduced")
    assert "corrupt" in r["error"]
    r = texttools.compare_bill_versions(cdbs, "AB 831",
                                        from_version="introduced",
                                        to_version="chaptered")
    assert "corrupt" in r["error"]


def test_guard_order_bound_fires_first(dbs, monkeypatch):
    """With the serving cap at zero the cheap character-difference bound
    must refuse BEFORE any diff work — its phrasing, not the work
    guard's or the output cap's."""
    monkeypatch.setattr(texttools, "_MAX_REDLINE_CHARS", 0)
    monkeypatch.setattr(texttools, "_MAX_PAIR_WORK", -1)
    r = texttools.compare_bill_versions(dbs, "AB 831",
                                        from_version="introduced",
                                        to_version="chaptered")
    body = r["body"]
    assert body["unavailable"] is True
    assert "share too little text" in body["statement"]


def test_work_guard_refuses_before_computing(dbs, monkeypatch):
    monkeypatch.setattr(texttools, "_MAX_PAIR_WORK", -1)
    r = texttools.compare_bill_versions(dbs, "AB 831",
                                        from_version="introduced",
                                        to_version="chaptered")
    body = r["body"]
    assert body["unavailable"] is True
    assert "pairing work" in body["statement"]
    assert body["identical"] is False
    # The digest part is still served in full.
    assert "unavailable" not in r["title_and_digest"]
    assert r["identical"] is False


def test_output_cap_serves_counts_instead(dbs, monkeypatch):
    """Cap set between the cheap char-difference bound (which must still
    pass) and the true markdown size, so exactly the output guard
    fires — and the refusal carries change counts."""
    r_full = texttools.compare_bill_versions(dbs, "AB 831",
                                             from_version="introduced",
                                             to_version="chaptered")
    md = r_full["body"]["redline_markdown"]
    cap = len(md) - 1
    monkeypatch.setattr(texttools, "_MAX_REDLINE_CHARS", cap)
    r = texttools.compare_bill_versions(dbs, "AB 831",
                                        from_version="introduced",
                                        to_version="chaptered")
    body = r["body"]
    assert body["unavailable"] is True
    assert "serving limit" in body["statement"]
    assert sum(body["change_counts"].values()) == \
        len(r_full["body"]["changes"])
    # A giant markdown with modest edits still serves the change list.
    assert body["changes"] == r_full["body"]["changes"]
    assert "included in full" in body["statement"]


# =========================================================================
# unit: ref parsing, hints, guards, index
# =========================================================================

@pytest.mark.parametrize(("text", "expect"), [
    ("current", {"kind": "current"}),
    ("Current Law", {"kind": "current"}),
    ("Stats. 2023, Ch. 534", {"kind": "chapter", "year": 2023,
                              "chapter": 534, "ex": 0, "hint": None}),
    ("Stats. 2023, Ch. 534, Sec. 1", {"kind": "chapter", "year": 2023,
                                      "chapter": 534, "ex": 0,
                                      "hint": "1"}),
    ("Stats. 2009, 3rd Ex. Sess., Ch. 17",
     {"kind": "chapter", "year": 2009, "chapter": 17, "ex": 3,
      "hint": None}),
    ("Section 2 of Chapter 285 of the Statutes of 2022",
     {"kind": "chapter", "year": 2022, "chapter": 285, "ex": 0,
      "hint": "2"}),
    ("2023 ch 534", {"kind": "chapter", "year": 2023, "chapter": 534,
                     "ex": 0, "hint": None}),
    ("Ch. 534, 2023", {"kind": "chapter", "year": 2023, "chapter": 534,
                       "ex": 0, "hint": None}),
    ("chapter 534 of 2023", {"kind": "chapter", "year": 2023,
                             "chapter": 534, "ex": 0, "hint": None}),
    ("AB 405", {"kind": "measure", "measure": "AB 405"}),
    ("Ch. 534", None),       # no year: not a resolvable chapter ref
    ("the vibes", None),
    ("Stats. 2023, Ch. " + "9" * 4500, None),  # digit bomb: no int()
    ("AB " + "9" * 4500, None),
])
def test_parse_ref(text, expect):
    assert texttools._parse_ref(text) == expect


def test_note_hints_reads_both_citations():
    hints = texttools._note_hints(_54953_NOTE)
    assert hints == {(2024, 389, 0): "1", (2023, 534, 0): "1"}


def test_heading_num_ignores_the_dot_in_sec():
    """'SEC. 3.' must yield 3, not match the abbreviation's own dot —
    the bug class that silently disabled hint matching for every
    SEC.-style heading."""
    assert texttools._HEAD_NUM.search("SEC. 3.").group(0) == "3."
    assert texttools._HEAD_NUM.search("SECTION 1.").group(0) == "1."
    assert texttools._HEAD_NUM.search("SEC. 1.5.").group(0) == "1.5."


def test_min_redline_chars_bounds():
    assert texttools._min_redline_chars("same words", "same words") == 0
    # Disjoint alphabets: only the space characters can match, so the
    # bound must recover 80% of the 20k total as guaranteed-changed.
    a, b = "aaaa " * 2000, "zzzz " * 2000
    assert texttools._min_redline_chars(a, b) == 16_000


def test_sec_index_logs_suspected_missplit(caplog):
    """A literal 'SEC. n' inside quoted statutory text breaks the
    strictly-increasing heading sequence — log it (SPEC §13 residual
    risk), never crash."""
    flat = ("The people of the State of California do enact as follows:"
            "SECTION 1.Section 5 of the Demo Code is amended to read:"
            "5. Body quoting: SEC. 2.Section 7 of the Demo Code reads "
            "oddly here.SEC. 2. Section 7 of the Demo Code is amended to "
            "read:7. Real text.")
    with caplog.at_level("WARNING", logger="server.texttools"):
        entries = texttools._sec_index(flat, "TESTVID")
    assert any("suspected SEC-split false positive" in m
               for m in caplog.messages)
    assert entries  # degraded, not fatal


def test_sec_index_uncodified_block_first_sentence():
    flat = ("do enact as follows:SECTION 1.The Legislature finds and "
            "declares all of the following: (a) Things. (b) More things.")
    (entry,) = texttools._sec_index(flat, "TESTVID")
    assert entry["intro"].startswith("SECTION 1.The Legislature finds")
    assert len(entry["intro"]) <= 200


def test_sec_index_clips_long_intros_at_200():
    long_tail = "It is the intent of the Legislature that " * 12
    flat = f"do enact as follows:SEC. 5.{long_tail}shall apply. More."
    (entry,) = texttools._sec_index(flat, "TESTVID")
    assert len(entry["intro"]) == 200
    assert entry["intro"].endswith("…")


def test_digit_bombs_return_error_dicts(dbs):
    big = "9" * 5000
    r = texttools.get_bill_text(dbs, f"AB {big}")
    assert "error" in r
    r = texttools.compare_section_versions(dbs, "GOV", "54953",
                                           from_ref=f"ch {big} of 2023")
    assert "error" in r
    r = texttools.compare_bill_versions(dbs, f"SB {big}")
    assert "error" in r

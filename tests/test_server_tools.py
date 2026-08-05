"""Integration tests: the seven tools against fixture-built databases
(the same mini current.db and 1989+1999 archive the ingest tests use).

Every response must carry the envelope (extract dates + source note) and
follow the never-empty-and-silent error contract.
"""


import pytest

from ingest.archive import build_archive_db
from ingest.build import build_current_db
from server import tools
from server.db import Databases
from server.history import HistoryEvent


@pytest.fixture(scope="module")
def dbs(mini_zip, archive_zips_dir, tmp_path_factory) -> Databases:
    d = tmp_path_factory.mktemp("server_dbs")
    build_current_db(mini_zip, d / "current.db")
    build_archive_db(
        sorted(archive_zips_dir.glob("pubinfo_*.zip")), d / "archive.db")
    return Databases(d / "current.db", d / "archive.db")


@pytest.fixture(scope="module")
def dbs_no_archive(dbs, tmp_path_factory) -> Databases:
    return Databases(dbs.current_path, None)


def assert_envelope(r):
    assert r["law_extract_date"]
    assert r["bill_extract_date"]
    assert "bulk-data downloads" in r["source"]
    assert r["current_session"] == "2025-2026"


# --- tool 1: get_section -------------------------------------------------

def test_get_section(dbs):
    r = tools.get_section(dbs, "Ed. Code", "Section 44955")
    assert_envelope(r)
    assert r["code"] == "EDC" and r["section"] == "44955"
    assert len(r["versions"]) == 1
    v = r["versions"][0]
    assert "certificated employees" in v["text"]
    assert v["history_note"].startswith("Amended by Stats. 1983")
    assert isinstance(r["hierarchy"], list)


def test_get_section_cons(dbs):
    r = tools.get_section(dbs, "Cal. Const.", "Article I, Section 3")
    assert_envelope(r)
    assert r["section"] == "Art. I, Sec. 3"
    assert r["versions"][0]["text"]
    assert any("ARTICLE I" in h for h in r["hierarchy"])


def test_get_section_unknown_code(dbs):
    r = tools.get_section(dbs, "Klingon Code", "1")
    assert "error" in r and r["suggestions"]


def test_get_section_miss_has_suggestions(dbs):
    r = tools.get_section(dbs, "EDC", "44955.99")
    assert "error" in r
    assert "44955" in r["suggestions"]


def test_get_section_cons_article_only_lists_sections(dbs):
    r = tools.get_section(dbs, "CONS", "Article I")
    assert "error" in r
    assert "Art. I, Sec. 1" in r["sections_in_article"]


def test_get_section_cons_unscoped(dbs):
    r = tools.get_section(dbs, "CONS", "3")
    assert "error" in r and "article-scoped" in r["error"]


# --- tool 2: search_sections ---------------------------------------------

def test_search_sections(dbs):
    r = tools.search_sections(dbs, "certificated employees")
    assert_envelope(r)
    hits = [(x["code"], x["section"]) for x in r["results"]]
    assert ("EDC", "44955") in hits
    assert all(x["snippet"] for x in r["results"])


def test_search_sections_code_filter(dbs):
    r = tools.search_sections(dbs, "certificated employees", code="PEN")
    assert ("EDC", "44955") not in [
        (x["code"], x["section"]) for x in r["results"]]


def test_search_sections_bad_fts_syntax_retried(dbs):
    r = tools.search_sections(dbs, 'employees AND (')
    assert "error" not in r
    assert any("literal quoted terms" in n for n in r.get("notes", []))


def test_search_sections_no_hits_is_explained(dbs):
    r = tools.search_sections(dbs, "zyzzogeton")
    assert r["results"] == []
    assert any("No matches" in n for n in r["notes"])


# --- tool 3: bills_affecting_section -------------------------------------

def test_bills_affecting_section(dbs):
    r = tools.bills_affecting_section(dbs, "Pen. Code", "1050")
    assert_envelope(r)
    measures = {b["measure"] for b in r["bills"]}
    assert measures == {"AB 1656", "AB 2052"}
    assert all(b["pending"] for b in r["bills"])
    assert all(ref["match"] == "direct"
               for b in r["bills"] for ref in b["references"])


def test_bills_affecting_section_none_is_noted(dbs):
    r = tools.bills_affecting_section(dbs, "EDC", "44955")
    assert r["bills"] == []
    assert any("No bills" in n for n in r["notes"])


def test_bills_affecting_missing_section_flagged(dbs):
    # Not in the mini law slice; refs to absent sections still surface.
    r = tools.bills_affecting_section(dbs, "GOV", "99999")
    assert any("not in current law" in n for n in r["notes"])


# --- tool 4: get_bill ----------------------------------------------------

def test_get_bill_current(dbs):
    r = tools.get_bill(dbs, "AB 831")
    assert_envelope(r)
    b = r["bill"]
    assert b["chapter"] == "Stats. 2025, Ch. 623"
    assert b["status"] == "Chaptered" and not b["pending"]
    assert b["authors"] and b["history"] and b["versions"]
    assert any(a["primary"] for a in b["authors"])
    assert r["from"] == "current"


def test_get_bill_by_bill_id(dbs):
    r = tools.get_bill(dbs, "202520260AB831")
    assert r["bill"]["measure"] == "AB 831"


def test_get_bill_archive(dbs):
    r = tools.get_bill(dbs, "SB 729", "1989-1990")
    assert r["from"] == "archive"
    assert r["bill"]["chapter"] == "Stats. 1989, Ch. 585"
    assert any(s["code"] == "WAT" and s["section"] == "20200"
               for s in r["bill"]["sections_affected"])
    # 1989 era limits are stated, not silent.
    assert any("no committee/floor analyses" in n for n in r["notes"])


def test_get_bill_ex_session_disambiguation(dbs):
    # AB 1 exists in both the 1999-2000 regular and 1st ex. sessions.
    r = tools.get_bill(dbs, "AB 1", "1999-2000")
    assert r["bill"]["bill_id"] == "199920000AB1"
    assert r["additional_matches"][0]["bill_id"] == "199920001AB1"
    assert any("extraordinary" in n for n in r["notes"])


def test_get_bill_not_found(dbs):
    r = tools.get_bill(dbs, "AB 9999")
    assert "not found" in r["error"]


def test_get_bill_pre_1989(dbs):
    r = tools.get_bill(dbs, "AB 1", "1971-1972")
    assert "predates" in r["error"]


def test_get_bill_vetoed_has_message_row(dbs):
    r = tools.get_bill(dbs, "SB 275")
    assert r["bill"]["status"] == "Vetoed"
    assert r["bill"]["veto_messages"][0]["veto_date"]


def test_get_bill_no_archive_degrades(dbs_no_archive):
    r = tools.get_bill(dbs_no_archive, "SB 729", "1989-1990")
    assert "archive.db is not available" in r["error"]


def test_get_bill_archive_never_pending(dbs):
    # Archived bills keep the last status of their session; the pending
    # flag must not resurrect them.
    r = tools.get_bill(dbs, "AB 1", "1999-2000")
    assert r["bill"]["pending"] is False
    assert all(m["pending"] is False for m in r["additional_matches"])


def test_get_bill_archive_has_votes_key(dbs):
    r = tools.get_bill(dbs, "SB 729", "1989-1990")
    assert isinstance(r["bill"]["votes"], list)  # empty in the 1989 era
    r = tools.get_bill(dbs, "AB 831")
    assert "votes" not in r["bill"]  # vote tables are archive-only


def test_get_bill_miss_states_era_coverage(dbs):
    # A miss in a chaptered-only era means "not enacted", not "never
    # existed" — the error must say so.
    r = tools.get_bill(dbs, "AB 9999", "1989-1990")
    assert "not found" in r["error"]
    assert any("chaptered" in c for c in r["coverage"])


# --- tool 5: get_bill_analyses -------------------------------------------

def test_get_bill_analyses_index_and_text(dbs):
    r = tools.get_bill_analyses(dbs, "AB 1", "1999-2000")
    assert_envelope(r)
    assert r["analyses"], "1999 AB 1 fixture has analyses"
    with_text = [a for a in r["analyses"] if a["has_text"]]
    assert with_text
    t = tools.get_bill_analyses(dbs, analysis_id=with_text[0]["analysis_id"])
    assert t["from"] == "archive"
    assert t["text"] and len(t["text"]) > 100
    assert t["bill"]["measure"] == "AB 1"


def test_get_bill_analyses_current(dbs):
    r = tools.get_bill_analyses(dbs, "AB 831")
    assert r["from"] == "current"
    assert len(r["analyses"]) >= 3


def test_get_bill_analyses_unknown_id(dbs):
    r = tools.get_bill_analyses(dbs, analysis_id="999999999")
    assert "not found" in r["error"]


def test_get_bill_analyses_needs_input(dbs):
    r = tools.get_bill_analyses(dbs)
    assert "error" in r


def test_analysis_miss_honest_without_archive(dbs_no_archive):
    r = tools.get_bill_analyses(dbs_no_archive, analysis_id="999999999")
    assert "archive.db is not available" in r["error"]


def test_ambiguity_note_lists_all_bills():
    rows = [("199920000AB1",) + (None,) * 12,
            ("200120020SB2",) + (None,) * 12]
    note = tools._ambiguity_note(rows)
    assert "199920000AB1" in note and "200120020SB2" in note
    assert tools._ambiguity_note(rows[:1]) is None


# --- tool 6: get_legislative_history -------------------------------------

def test_history_pre_1989_marked(dbs):
    r = tools.get_legislative_history(dbs, "EDC", "44955")
    assert_envelope(r)
    assert r["history_notes"]
    ev = r["events"][0]
    assert ev["kind"] == "chapter" and "1983" in ev["citation"]
    assert ev["resolution"] == "predates_electronic_records"


def test_history_cons_resolution_chapter_unresolved_when_absent(dbs):
    # Art. I Sec. 3 cites Res.Ch. 123, 2013 — no 2013 session in the
    # fixture archive, so the event is explicitly unresolved, not silent.
    r = tools.get_legislative_history(dbs, "CONS", "Art. I, Sec. 3")
    ev = r["events"][0]
    assert ev["kind"] == "resolution_chapter"
    assert ev["resolution"] == "unresolved"
    assert ev["proposition"] == "42"


def test_history_section_missing_suggestions(dbs):
    r = tools.get_legislative_history(dbs, "EDC", "44955.99")
    assert "error" in r and r["suggestions"]


def test_resolve_event_current_session_chapter(dbs):
    # A Stats. 2025 chapter resolves against current.db (AB 831).
    with dbs.current() as con:
        ev = HistoryEvent(kind="chapter", citation="Stats. 2025, Ch. 623",
                          year=2025, chapter=623)
        out = tools._resolve_event(dbs, con, ev, [])
    assert out["resolution"] == "resolved"
    assert out["bill"]["measure"] == "AB 831"
    assert out["from"] == "current"


def test_resolve_event_archive_chapter(dbs):
    with dbs.current() as con:
        ev = HistoryEvent(kind="chapter", citation="Stats. 1989, Ch. 585",
                          year=1989, chapter=585)
        out = tools._resolve_event(dbs, con, ev, [])
    assert out["resolution"] == "resolved"
    assert out["bill"]["measure"] == "SB 729"
    assert any("no committee/floor analyses" in n
               for n in out.get("coverage", []))


def test_resolve_event_measure_hint_mismatch_warns(dbs):
    with dbs.current() as con:
        ev = HistoryEvent(kind="chapter", citation="Stats. 2025, Ch. 623",
                          year=2025, chapter=623, measure_hint="SB 111")
        out = tools._resolve_event(dbs, con, ev, [])
    assert "flagging for review" in out["warning"]


def test_bills_citing_lineage(dbs):
    # WAT 20200 was amended by 1989's SB 729 — the title-based lineage
    # finds it even though the section isn't in the mini law slice.
    with dbs.current() as con:
        citing = tools._bills_citing(dbs, con, "WAT", "20200", None)
    assert [(c["measure"], c["chapter"]) for c in citing] == \
        [("SB 729", "Stats. 1989, Ch. 585")]


def test_history_no_archive_notes_limit(dbs_no_archive):
    r = tools.get_legislative_history(dbs_no_archive, "EDC", "100850")
    assert any("archive.db is not available" in n for n in r["notes"])


# --- tool 7: chapter_to_bill ---------------------------------------------

def test_chapter_to_bill_current(dbs):
    r = tools.chapter_to_bill(dbs, 2025, 623)
    assert_envelope(r)
    assert r["bill"]["measure"] == "AB 831"
    assert r["from"] == "current"


def test_chapter_to_bill_archive(dbs):
    r = tools.chapter_to_bill(dbs, 1989, 585)
    assert r["bill"]["measure"] == "SB 729"
    assert r["from"] == "archive"


def test_chapter_to_bill_resolution(dbs):
    r = tools.chapter_to_bill(dbs, 1989, 90, kind="resolution")
    assert r["bill"]["measure"] == "ACR 65"
    assert r["chapter"].startswith("Res. Ch. 90")


def test_chapter_to_bill_ex_session_hint(dbs):
    # 1999 Ch. 4 exists only in the 1st ex. session; the regular-session
    # miss must hint at it rather than dead-end.
    r = tools.chapter_to_bill(dbs, 1999, 4)
    assert "error" in r
    assert any("ex_session=1" in h for h in r["hint"])
    r2 = tools.chapter_to_bill(dbs, 1999, 4, ex_session=1)
    assert r2["bill"]["bill_id"] == "199920001AB1"


def test_chapter_to_bill_pre_1989(dbs):
    r = tools.chapter_to_bill(dbs, 1971, 1)
    assert "predate" in r["error"]


def test_chapter_to_bill_coverage_statement(dbs):
    r = tools.chapter_to_bill(dbs, 2013, 99999)
    assert "error" in r
    assert "Archive covers" in r["coverage"]

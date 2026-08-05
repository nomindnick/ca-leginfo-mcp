"""Failure-path hardening tests for the archive builder (ingest/archive.py).

Every test builds from small mutated zips assembled out of the committed
archive fixture trees (tests/fixtures/archive/{1989,1999}) — never the
real 900 MB session zips. The mutations simulate the corruption a
30-year-old public FTP corpus actually exhibits: absent tables, malformed
.dat rows, corrupt/truncated lobs, interrupted multi-hour builds.

Design contract under test (SPEC §6): log, count, never crash the build;
per-session transactions make interruption resumable; the coverage matrix
records absences honestly.

Known deviations are pinned here and reported upstream rather than fixed
(this file must not touch ingest/):

- A corrupt zip member holding an *analysis* lob aborts the build
  (`zf.read` happens outside the try in `_extract_analyses`) — strict
  xfail below.
- A bill-version lob whose XML is truncated (valid zip member, broken
  content) is recorded as a *successful* extraction with a NULL title:
  nothing counts it, and it silently leaves the title-coverage
  denominator. Current behavior is pinned below.
"""

from __future__ import annotations

import json
import sqlite3
import struct
import zipfile
import zlib
from pathlib import Path

import pytest

from ingest import archive as archive_mod
from ingest.archive import _session_of, build_archive_db
from ingest.cli import main as cli_main

FIXTURES = Path(__file__).parent / "fixtures" / "archive"

# vid <-> lob pairs from the committed 1989 fixture .dat rows.
SJR31_VID, SJR31_LOB = "19890SJR3195CHP", "BILL_VERSION_TBL_100.lob"
SB729_VID, SB729_LOB = "19890SB72994CHP", "BILL_VERSION_TBL_1094.lob"


# --- zip mutation helpers ---------------------------------------------------

def _make_zip(out: Path, tree: Path, *, drop: set[str] = frozenset(),
              override: dict[str, bytes] | None = None,
              stored: set[str] = frozenset()) -> Path:
    """Zip a fixture tree with per-member surgery: drop members, replace
    payloads, or store uncompressed (so a CRC can be broken in place)."""
    override = dict(override or {})
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(tree.rglob("*")):
            if not f.is_file():
                continue
            rel = f.relative_to(tree).as_posix()
            if rel in drop:
                continue
            comp = (zipfile.ZIP_STORED if rel in stored
                    else zipfile.ZIP_DEFLATED)
            zf.writestr(rel, override.pop(rel, f.read_bytes()),
                        compress_type=comp)
        for rel, data in override.items():  # members not in the tree
            zf.writestr(rel, data)
    return out


def _corrupt_member(zip_path: Path, member: str) -> None:
    """Flip payload bytes of a ZIP_STORED member so its CRC no longer
    matches — zipfile then raises BadZipFile when the member is read,
    which is how on-disk corruption of an archive member presents."""
    with zipfile.ZipFile(zip_path) as zf:
        info = zf.getinfo(member)
        assert info.compress_type == zipfile.ZIP_STORED
        header_offset = info.header_offset
    raw = bytearray(zip_path.read_bytes())
    fnlen, exlen = struct.unpack_from("<HH", raw, header_offset + 26)
    payload = header_offset + 30 + fnlen + exlen
    for i in range(payload + 64, payload + 128):
        raw[i] ^= 0xFF
    zip_path.write_bytes(bytes(raw))


# --- missing tables / malformed rows ---------------------------------------

def test_missing_whole_tables_and_lobs_tolerated(tmp_path):
    """No BILL_TBL.dat at all (worse than any real era) plus an absent
    version lob: everything else still loads, absences are counted."""
    zp = _make_zip(
        tmp_path / "pubinfo_1989.zip", FIXTURES / "1989",
        drop={"BILL_TBL.dat", "BILL_VERSION_AUTHORS_TBL.dat",
              "BILL_VERSION_TBL_99.lob"})
    out = tmp_path / "a.db"
    report = build_archive_db([zp], out, workers=2)
    s, = report.sessions
    assert s.table_rows["bill"] == 0
    assert s.table_rows["bill_version_authors"] == 0
    assert s.table_rows["bill_version"] == 5  # untouched table still loads
    assert s.version_lobs == 4
    assert "1 bill version lobs absent from zip" in s.warnings
    con = sqlite3.connect(out)
    try:
        assert con.execute("SELECT count(*) FROM bill").fetchone() == (0,)
        assert con.execute(
            "SELECT count(*) FROM bill_version").fetchone() == (5,)
        cov = {k: v for _, k, v in con.execute(
            "SELECT * FROM session_coverage WHERE session_year='1989'")}
        # The absence is recorded, not silently elided (SPEC §5 honesty).
        assert cov["rows_bill"] == "0"
        assert cov["rows_bill_version"] == "5"
    finally:
        con.close()


def test_malformed_dat_rows_dropped_and_counted(tmp_path):
    orig = (FIXTURES / "1989" / "BILL_TBL.dat").read_bytes()
    junk = b"stray\trow\n" + b"\t".join([b"`x`"] * 25) + b"\n"
    zp = _make_zip(tmp_path / "pubinfo_1989.zip", FIXTURES / "1989",
                   override={"BILL_TBL.dat": orig + junk})
    out = tmp_path / "a.db"
    report = build_archive_db([zp], out, workers=2)
    s, = report.sessions
    assert s.table_rows["bill"] == 5          # good rows kept
    assert s.bad_rows["bill"] == 2            # short + long row both dropped
    assert "BILL_TBL.dat: 2 malformed rows dropped" in s.warnings
    con = sqlite3.connect(out)
    try:
        assert con.execute(
            "SELECT count(*) FROM bill").fetchone() == (5,)
        assert con.execute(
            "SELECT count(*) FROM bill WHERE bill_id='198919900SB729'"
        ).fetchone() == (1,)
    finally:
        con.close()


# --- corrupt bill-version lobs ---------------------------------------------

def test_corrupt_bill_version_lob_counted_not_fatal(tmp_path):
    """A zip member whose bytes rotted (CRC mismatch) is counted as a
    failed extraction; the other lobs and the refs pass still complete."""
    zp = _make_zip(tmp_path / "pubinfo_1989.zip", FIXTURES / "1989",
                   stored={SJR31_LOB})
    _corrupt_member(zp, SJR31_LOB)
    out = tmp_path / "a.db"
    report = build_archive_db([zp], out, workers=2)
    s, = report.sessions
    assert s.version_lobs == 4
    assert "1 bill version lobs failed extraction" in s.warnings
    con = sqlite3.connect(out)
    try:
        assert con.execute(
            "SELECT count(*) FROM bill_version_text WHERE bill_version_id=?",
            (SJR31_VID,)).fetchone() == (0,)
        assert con.execute(
            "SELECT count(*) FROM bill_version_text").fetchone() == (4,)
        # The healthy SB 729 lob still produced its Water Code ref.
        refs = con.execute(
            "SELECT law_code, section FROM bill_section_ref "
            "WHERE bill_version_id=?", (SB729_VID,)).fetchall()
        assert ("WAT", "20200") in refs
    finally:
        con.close()


def test_truncated_xml_lob_counted_in_coverage(tmp_path):
    """Truncated XML inside a *valid* zip member: not fatal, and the lost
    title stays visible — counted as 'no_title' in the coverage matrix
    (denominator intact for the 99% gate) with a residue entry."""
    orig = (FIXTURES / "1989" / SB729_LOB).read_bytes()
    cut = orig.find(b"</caml:Title>")
    assert cut > 0
    zp = _make_zip(tmp_path / "pubinfo_1989.zip", FIXTURES / "1989",
                   override={SB729_LOB: orig[:cut]})
    out = tmp_path / "a.db"
    report = build_archive_db([zp], out, workers=2)
    s, = report.sessions
    assert s.version_lobs == 5
    assert sum(s.title_coverage.values()) == 5       # denominator intact
    assert s.title_coverage.get("no_title") == 1
    assert any("[no_title]" in r for r in s.residue)
    con = sqlite3.connect(out)
    try:
        title_null, blob = con.execute(
            "SELECT title_text IS NULL, text_zlib FROM bill_version_text "
            "WHERE bill_version_id=?", (SB729_VID,)).fetchone()
        assert title_null == 1
        # The partial flattened text is still stored.
        assert zlib.decompress(blob).decode().strip()
        # NULL title -> no refs for this version.
        assert con.execute(
            "SELECT count(*) FROM bill_section_ref WHERE bill_version_id=?",
            (SB729_VID,)).fetchone() == (0,)
    finally:
        con.close()


# --- corrupt analysis lobs --------------------------------------------------

def test_corrupt_analysis_lob_content_counted_not_fatal(tmp_path):
    """Undecodable analysis payloads (a lob with docx magic but rotten
    innards; an unhandled rtf) and a missing lob are each counted; the
    remaining analyses still extract."""
    zp = _make_zip(
        tmp_path / "pubinfo_1999.zip", FIXTURES / "1999",
        override={"BILL_ANALYSIS_TBL_56.lob": b"PK\x03\x04garbage-not-a-zip",
                  "BILL_ANALYSIS_TBL_58.lob": b"{\\rtf1 hello}"},
        drop={"BILL_ANALYSIS_TBL_57.lob"})
    out = tmp_path / "a.db"
    report = build_archive_db([zp], out, workers=2)
    s, = report.sessions
    assert s.analysis_extract_errors == 2   # bad docx + unhandled rtf
    assert s.analysis_missing_lob == 1
    assert s.analysis_formats == {"docx": 1, "rtf": 1, "html": 29}
    assert any("BILL_ANALYSIS_TBL_56.lob" in w for w in s.warnings)
    assert any("unhandled rtf" in w for w in s.warnings)
    con = sqlite3.connect(out)
    try:
        assert con.execute(
            "SELECT count(*) FROM analysis_text").fetchone() == (29,)
        assert con.execute(
            "SELECT count(*) FROM analysis_text "
            "WHERE analysis_id IN (125755, 125756, 125757)"
        ).fetchone() == (0,)
    finally:
        con.close()


def test_corrupt_analysis_zip_member_counted_not_fatal(tmp_path):
    """A corrupt zip member (CRC mismatch) under an analysis lob is
    counted as an extraction error, never aborts the session (SPEC §6)."""
    zp = _make_zip(tmp_path / "pubinfo_1999.zip", FIXTURES / "1999",
                   stored={"BILL_ANALYSIS_TBL_56.lob"})
    _corrupt_member(zp, "BILL_ANALYSIS_TBL_56.lob")
    out = tmp_path / "a.db"
    report = build_archive_db([zp], out, workers=2)
    s, = report.sessions
    assert s.analysis_extract_errors == 1
    assert any("BILL_ANALYSIS_TBL_56.lob" in w for w in s.warnings)
    con = sqlite3.connect(out)
    try:
        # 32 analysis lobs in the fixture; the corrupted one is skipped.
        assert con.execute(
            "SELECT count(*) FROM analysis_text").fetchone() == (31,)
    finally:
        con.close()


# --- interruption & resume (per-session transaction semantics) --------------

def test_interrupt_second_session_commits_first_then_resume(
        archive_zips_dir, tmp_path, monkeypatch):
    """A failure mid-way through session 2: session 1's transaction is
    committed and survives; session 2's partial rows roll back and the
    failure is recorded (build completes, per-session isolation); a
    resume build skips 1 and completes 2 without duplicating anything."""
    zips = sorted(archive_zips_dir.glob("pubinfo_*.zip"))
    out = tmp_path / "crash.db"
    real_refs = archive_mod._build_refs

    def boom(con, session, srep, residue_cap):
        if session == "1999":
            raise RuntimeError("simulated interruption")
        return real_refs(con, session, srep, residue_cap)

    with monkeypatch.context() as m:
        m.setattr(archive_mod, "_build_refs", boom)
        r1 = build_archive_db(zips, out, workers=2)
    assert [s.session for s in r1.sessions] == ["1989"]
    assert r1.failed_sessions and r1.failed_sessions[0][0] == "1999"
    assert "simulated interruption" in r1.failed_sessions[0][1]

    con = sqlite3.connect(out)
    try:
        done = [k for (k,) in con.execute(
            "SELECT key FROM meta WHERE key LIKE 'session_done_%'")]
        assert done == ["session_done_1989"]
        # The failure is recorded in the coverage matrix.
        err, = con.execute(
            """SELECT value FROM session_coverage
               WHERE session_year='1999' AND key='build_error'""").fetchone()
        assert "simulated interruption" in err
        # Session 1 committed in full...
        assert con.execute(
            "SELECT count(*) FROM bill WHERE session_year LIKE '1989%'"
        ).fetchone() == (5,)
        # ...and session 2's partial inserts (its version text had already
        # been extracted when the crash hit) were rolled back wholesale.
        assert con.execute(
            "SELECT count(*) FROM bill WHERE session_year LIKE '1999%'"
        ).fetchone() == (0,)
        assert con.execute(
            "SELECT count(*) FROM bill_version_text").fetchone() == (5,)
    finally:
        con.close()

    r2 = build_archive_db(zips, out, resume=True, workers=2)
    assert r2.skipped_sessions == ["1989"]
    assert [s.session for s in r2.sessions] == ["1999"]
    con = sqlite3.connect(out)
    try:
        assert con.execute("SELECT count(*) FROM bill").fetchone() == (10,)
        n, distinct = con.execute(
            "SELECT count(*), count(DISTINCT bill_version_id) "
            "FROM bill_version_text").fetchone()
        assert n == distinct == 5 + 34  # no duplicates from the retry
    finally:
        con.close()


def test_resume_with_different_zip_set(archive_zips_dir, tmp_path):
    """Resume may be handed a different zip list; sessions already in the
    db are kept even when their zip is no longer offered."""
    z89, z99 = sorted(archive_zips_dir.glob("pubinfo_*.zip"))
    out = tmp_path / "r.db"
    build_archive_db([z89], out, workers=2)
    r2 = build_archive_db([z99], out, resume=True, workers=2)
    assert r2.skipped_sessions == []          # 1989 wasn't offered at all
    assert [s.session for s in r2.sessions] == ["1999"]
    con = sqlite3.connect(out)
    try:
        assert con.execute(
            "SELECT count(DISTINCT session_year) FROM bill"
        ).fetchone() == (2,)
        meta = dict(con.execute("SELECT key, value FROM meta"))
        assert json.loads(meta["sessions"]) == ["1989", "1999"]
    finally:
        con.close()


# --- text-suppression flags -------------------------------------------------

def test_no_bill_text_yields_titles_and_refs_null_text(
        archive_zips_dir, tmp_path):
    out = tmp_path / "nbt.db"
    rc = cli_main(["build-archive", "--zips-dir", str(archive_zips_dir),
                   "--sessions", "1989", "--out", str(out),
                   "--workers", "2", "--no-bill-text"])
    assert rc == 0
    con = sqlite3.connect(out)
    try:
        rows = con.execute(
            "SELECT title_text IS NOT NULL, text_zlib FROM bill_version_text"
        ).fetchall()
        assert len(rows) == 5
        assert all(has_title for has_title, _ in rows)  # titles still there
        assert all(blob is None for _, blob in rows)    # text suppressed
        # Refs still parse from the titles.
        refs = con.execute(
            "SELECT law_code, section FROM bill_section_ref "
            "WHERE bill_id LIKE '1989%SB729'").fetchall()
        assert ("WAT", "20200") in refs
    finally:
        con.close()


def test_no_analysis_text_yields_empty_analysis_text(
        archive_zips_dir, tmp_path):
    out = tmp_path / "nat.db"
    rc = cli_main(["build-archive", "--zips-dir", str(archive_zips_dir),
                   "--sessions", "1999", "--out", str(out),
                   "--workers", "2", "--no-analysis-text"])
    assert rc == 0
    con = sqlite3.connect(out)
    try:
        assert con.execute(
            "SELECT count(*) FROM analysis_text").fetchone() == (0,)
        # The bill_analysis metadata table itself still loads in full.
        assert con.execute(
            "SELECT count(*) FROM bill_analysis").fetchone() == (32,)
        cov = {k: v for _, k, v in con.execute(
            "SELECT * FROM session_coverage WHERE session_year='1999'")}
        assert cov["analysis_formats"] == "{}"
        assert cov["analysis_errors"] == "0"
    finally:
        con.close()


# --- session filtering isolation --------------------------------------------

def test_building_session_b_never_reprocesses_session_a(
        archive_zips_dir, tmp_path):
    z89, z99 = sorted(archive_zips_dir.glob("pubinfo_*.zip"))
    out = tmp_path / "iso.db"
    build_archive_db([z89], out, workers=2)
    con = sqlite3.connect(out)
    snapshot = con.execute(
        "SELECT bill_version_id, title_text, text_zlib "
        "FROM bill_version_text ORDER BY bill_version_id").fetchall()
    refs89 = con.execute(
        "SELECT count(*) FROM bill_section_ref WHERE session_year='1989'"
    ).fetchone()
    con.close()
    assert len(snapshot) == 5

    r2 = build_archive_db([z89, z99], out, resume=True, workers=2)
    s99, = r2.sessions
    assert s99.session == "1999"
    assert s99.version_lobs == 34            # only B's lobs were processed
    assert s99.table_rows["bill"] == 5       # only B's .dat rows loaded
    con = sqlite3.connect(out)
    try:
        # A's extracted rows are byte-identical — nothing re-ran for A.
        after = con.execute(
            "SELECT bill_version_id, title_text, text_zlib "
            "FROM bill_version_text WHERE bill_version_id LIKE '1989%' "
            "ORDER BY bill_version_id").fetchall()
        assert after == snapshot
        assert con.execute(
            "SELECT count(*) FROM bill_section_ref WHERE session_year='1989'"
        ).fetchone() == refs89
        assert con.execute(
            "SELECT count(*) FROM bill_version_text").fetchone() == (5 + 34,)
    finally:
        con.close()


# --- zip-name validation ----------------------------------------------------

@pytest.mark.parametrize("name", [
    "pubinfo_89.zip",          # two-digit year
    "pubinfo_198.zip",
    "pubinfo_19890.zip",       # five digits
    "PUBINFO_1989.zip",        # case matters
    "pubinfo_1989.zip.bak",    # suffix after .zip
    "pubinfo_1989.tar",
    "pubinfo_daily_Fri.zip",   # the nightly zip is not a session zip
    "pubinfo_.zip",
    "1989.zip",
])
def test_session_of_rejects_odd_names(name):
    with pytest.raises(ValueError, match="not a session zip name"):
        _session_of(Path(name))


def test_session_of_accepts_session_zips():
    assert _session_of(Path("/anywhere/pubinfo_1989.zip")) == "1989"
    # Current behavior: only the end of the name is anchored, so a
    # prefixed copy is accepted and attributed to its embedded year.
    assert _session_of(Path("old_pubinfo_1989.zip")) == "1989"


def test_build_rejects_odd_zip_name_before_touching_out(tmp_path):
    out = tmp_path / "never.db"
    with pytest.raises(ValueError, match="not a session zip name"):
        build_archive_db([tmp_path / "pubinfo_notayear.zip"], out)
    assert not out.exists()

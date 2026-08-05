"""End-to-end tests for ingest/archive.py against real archive-era records:
1989 (chaptered-only era, incl. SB 729's mixed uncodified/code title) and
1999 (all versions, history, votes, HTML analyses)."""

import sqlite3
import zlib

import pytest

from ingest.archive import build_archive_db
from ingest.cli import main as cli_main


@pytest.fixture(scope="module")
def built(archive_zips_dir, tmp_path_factory):
    out = tmp_path_factory.mktemp("adb") / "archive.db"
    zips = sorted(archive_zips_dir.glob("pubinfo_*.zip"))
    report = build_archive_db(zips, out, workers=2)
    con = sqlite3.connect(out)
    yield con, report, out, zips
    con.close()


def test_sessions_built(built):
    con, report, _, _ = built
    assert [s.session for s in report.sessions] == ["1989", "1999"]
    meta = dict(con.execute("SELECT key, value FROM meta"))
    assert "session_done_1989" in meta and "session_done_1999" in meta


def test_1989_chaptered_only_era(built):
    _con, report, _, _ = built
    r89 = report.sessions[0]
    assert r89.table_rows["bill"] == 5
    # One version per bill in this era; every version got text.
    assert r89.table_rows["bill_version"] == 5
    assert r89.version_lobs == 5
    assert r89.table_rows["bill_history"] == 0  # era has no history


def test_uncodified_act_sections_not_misattributed(built):
    con, _, _, _ = built
    # SB 729 (1989) amends WAT 20200 plus sections of the Monterey
    # Peninsula Water Management District Law. The district-law sections
    # must not appear as refs; the Water Code ref must.
    refs = con.execute(
        """SELECT law_code, section FROM bill_section_ref
           WHERE bill_id LIKE '1989%SB729'""").fetchall()
    assert ("WAT", "20200") in refs
    assert all(code == "WAT" for code, _ in refs)


def test_1999_full_era_tables(built):
    _con, report, _, _ = built
    r99 = report.sessions[1]
    assert r99.table_rows["bill_history"] > 0
    assert r99.table_rows["bill_detail_vote"] > 0
    assert r99.table_rows["bill_summary_vote"] > 0
    # BILL_MOTION_TBL doesn't exist yet in the 1999 era (present by 2017) —
    # tolerated absence, recorded as 0 in the coverage matrix.
    assert r99.table_rows["bill_motion"] == 0
    assert r99.analysis_formats.get("html", 0) > 0
    assert r99.analysis_extract_errors == 0


def test_bill_text_starts_at_title(built):
    con, _, _, _ = built
    rows = con.execute(
        "SELECT text_zlib FROM bill_version_text WHERE text_zlib IS NOT NULL"
    ).fetchall()
    assert rows
    for (blob,) in rows[:10]:
        text = zlib.decompress(blob).decode()
        # Flattening starts at caml:Title — never at MeasureDoc metadata.
        assert text.startswith(("An act", "A resolution", "Relative to"))


def test_analysis_text_roundtrip(built):
    con, _, _, _ = built
    blob, = con.execute(
        """SELECT text_zlib FROM analysis_text LIMIT 1""").fetchone()
    text = zlib.decompress(blob).decode()
    assert len(text) > 200
    assert "\n\n\n" not in text  # blank-line runs collapsed


def test_session_coverage_matrix(built):
    con, _, _, _ = built
    cov = {(s, k): v for s, k, v in con.execute(
        "SELECT session_year, key, value FROM session_coverage")}
    assert cov[("1989", "rows_bill_history")] == "0"
    assert int(cov[("1999", "rows_bill_history")]) > 0
    assert ("1989", "title_coverage") in cov


def test_chapter_to_bill_pivot(built):
    con, _, _, _ = built
    rows = con.execute(
        """SELECT chapter_year, chapter_num, bill_id FROM bill
           WHERE chapter_num IS NOT NULL""").fetchall()
    assert rows
    y, ch, bid = rows[0]
    got, = con.execute(
        "SELECT bill_id FROM bill WHERE chapter_year=? AND chapter_num=?",
        (y, ch)).fetchone()
    assert got == bid


def test_resume_skips_done_sessions(built, tmp_path):
    _, _, _, zips = built
    out = tmp_path / "resume.db"
    r1 = build_archive_db(zips[:1], out, workers=2)
    assert [s.session for s in r1.sessions] == ["1989"]
    r2 = build_archive_db(zips, out, resume=True, workers=2)
    assert r2.skipped_sessions == ["1989"]
    assert [s.session for s in r2.sessions] == ["1999"]
    con = sqlite3.connect(out)
    n, = con.execute("SELECT count(DISTINCT session_year) FROM bill").fetchone()
    con.close()
    assert n == 2


def test_fresh_build_replaces_without_resume(built, tmp_path):
    _, _, _, zips = built
    out = tmp_path / "fresh.db"
    build_archive_db(zips[:1], out, workers=2)
    r = build_archive_db(zips[:1], out, workers=2)  # no resume: rebuild
    assert [s.session for s in r.sessions] == ["1989"]


def test_cli_build_archive(archive_zips_dir, tmp_path, capsys):
    out = tmp_path / "cli.db"
    rc = cli_main(["build-archive", "--zips-dir", str(archive_zips_dir),
                   "--sessions", "1989", "--out", str(out), "--workers", "2"])
    assert rc == 0 and out.exists()
    assert '"skipped_sessions": []' in capsys.readouterr().out


def test_cli_unknown_session_errors(archive_zips_dir, tmp_path):
    with pytest.raises(SystemExit):
        cli_main(["build-archive", "--zips-dir", str(archive_zips_dir),
                  "--sessions", "1955", "--out", str(tmp_path / "x.db")])

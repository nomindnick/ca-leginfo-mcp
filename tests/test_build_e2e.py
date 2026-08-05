"""End-to-end: mini pubinfo zip -> current.db, exercising every build stage
against real records (real .dat lines, real CAML lobs, real docx analyses)."""

import json
import sqlite3
import zlib

import pytest

from ingest.build import build_current_db


@pytest.fixture(scope="module")
def built(mini_zip, tmp_path_factory):
    out = tmp_path_factory.mktemp("db") / "current.db"
    report = build_current_db(mini_zip, out)
    con = sqlite3.connect(out)
    yield con, report, out
    con.close()


def test_tables_loaded(built):
    _con, report, _ = built
    assert report.table_rows["law_section"] > 40
    assert report.table_rows["bill"] == 4
    assert report.table_rows["codes"] == 30
    assert report.table_rows["bill_version_authors"] > 0


def test_statute_text_and_norm(built):
    con, _, _ = built
    text, = con.execute(
        """SELECT content_text FROM law_section
           WHERE law_code='EDC' AND section_num_norm='44955'""").fetchone()
    assert len(text) > 500 and "certificated employees" in text


def test_cons_article_scoped_key(built):
    con, _, _ = built
    rows = con.execute(
        """SELECT section_num_norm FROM law_section
           WHERE law_code='CONS'""").fetchall()
    assert rows and all(r[0].startswith("Art. ") for r in rows)


def test_bills_affecting_pen_1050(built):
    con, _, _ = built
    bills = {r[0] for r in con.execute(
        """SELECT DISTINCT b.measure_num FROM bill_section_ref r
           JOIN bill b ON b.bill_id = r.bill_id
           WHERE r.law_code='PEN' AND r.section='1050'
             AND r.bill_version_id = b.latest_bill_version_id""")}
    assert bills == {"2052", "1656"}


def test_exists_in_current_law_flag(built):
    con, _, _ = built
    # PEN 1050 is in the mini law set -> flagged as existing.
    flag, = con.execute(
        """SELECT DISTINCT exists_in_current_law FROM bill_section_ref
           WHERE law_code='PEN' AND section='1050'""").fetchone()
    assert flag == 1
    # The mini law slice omits most sections, so some refs must be flagged 0.
    n_missing, = con.execute(
        """SELECT count(*) FROM bill_section_ref
           WHERE exists_in_current_law=0""").fetchone()
    assert n_missing > 0


def test_analysis_text_roundtrip(built):
    con, report, _ = built
    assert report.analysis_formats.get("docx", 0) > 0
    _aid, fmt, blob = con.execute(
        """SELECT t.analysis_id, t.format, t.text_zlib
           FROM analysis_text t JOIN bill_analysis a USING (analysis_id)
           LIMIT 1""").fetchone()
    text = zlib.decompress(blob).decode()
    assert fmt == "docx" and len(text) > 200


def test_veto_message_row(built):
    con, _, _ = built
    n, = con.execute("SELECT count(*) FROM veto_message").fetchone()
    assert n >= 1


def test_meta_and_report(built):
    con, report, _ = built
    meta = dict(con.execute("SELECT key, value FROM meta"))
    assert meta["session_year"] == "20252026"
    assert meta["law_extract_date"] and meta["bill_extract_date"]
    assert json.loads(meta["title_coverage"])  # non-empty
    assert report.title_coverage.get("fail", 0) == 0


def test_fts(built):
    con, _, _ = built
    hits, = con.execute(
        """SELECT count(*) FROM law_fts
           WHERE law_fts MATCH 'certificated employees'""").fetchone()
    assert hits >= 1


def test_atomic_output_no_tmp_left(built):
    _, _, out = built
    assert out.exists()
    assert not out.with_name(out.name + ".tmp").exists()

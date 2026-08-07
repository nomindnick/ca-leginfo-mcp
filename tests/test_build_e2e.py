"""End-to-end: mini pubinfo zip -> current.db, exercising every build stage
against real records (real .dat lines, real CAML lobs, real docx analyses)."""

import json
import shutil
import sqlite3
import zipfile
import zlib

import pytest

from ingest import caml
from ingest.build import build_current_db


def _zip(tree, out):
    with zipfile.ZipFile(out, "w") as zf:
        for f in sorted(tree.rglob("*")):
            if f.is_file():
                zf.write(f, f.relative_to(tree).as_posix())
    return out


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


def test_bill_version_text_roundtrip(built):
    """V2 (SPEC §11): every version with a lob gets a bill_version_text
    row — title duplicated alongside (same shape as archive.db, so the
    V2 tools read either DB identically) and the full flattened text
    zlib-compressed, not just the title."""
    con, report, _ = built
    n_lob, = con.execute(
        """SELECT count(*) FROM bill_version
           WHERE lob_file IS NOT NULL""").fetchone()
    rows = con.execute(
        """SELECT v.title_text, t.title_text, t.text_zlib
           FROM bill_version v
           JOIN bill_version_text t USING (bill_version_id)""").fetchall()
    assert len(rows) == n_lob == report.title_lobs > 0
    for v_title, t_title, blob in rows:
        assert t_title == v_title
        text = zlib.decompress(blob).decode()
        assert "do enact as follows" in text  # body present, not just title
        # Flattened text opens with the title: ties each stored body to
        # the version it came from — a vid→body mis-pairing (worker
        # output misalignment) would satisfy any per-row content check.
        assert text.startswith(t_title)
    assert report.version_text_bytes == sum(len(r[2]) for r in rows) > 0
    # Same shape as archive.db (SPEC §11) — name order and PK pinned.
    cols = con.execute("PRAGMA table_info(bill_version_text)").fetchall()
    assert [(c[1], c[5]) for c in cols] == [
        ("bill_version_id", 1), ("title_text", 0), ("text_zlib", 0)]


def test_duplicate_vid_and_absent_lob(fixtures, tmp_path):
    """Source anomalies the extraction stage defends against, all absent
    from the clean corpus: a duplicated bill_version_id must leave every
    bill_version row titled from its OWN lob (not whichever extraction
    landed last); a lob named in the .dat but absent from the zip, and a
    lob_file that parses to the empty string, must both surface in the
    report warnings — they sit in the sanity gate's coverage
    denominator, so the report must account for them."""
    tree = tmp_path / "tree"
    shutil.copytree(fixtures / "mini", tree)
    dat = tree / "BILL_VERSION_TBL.dat"
    lines = [ln for ln in dat.read_text().splitlines() if ln.strip()]

    def field(ln: str, i: int) -> str:
        return ln.split("\t")[i].strip("`")

    # Append a row duplicating row 0's version id but carrying a
    # different bill's lob (their titles differ, so a fan-out that lets
    # one extraction overwrite the other is observable).
    other = next(ln for ln in lines[1:] if field(ln, 1) != field(lines[0], 1))
    dup = lines[0].split("\t")
    dup[1] = other.split("\t")[1]
    dup[14] = other.split("\t")[14]
    # Strand a third row: keep its .dat row, delete its lob file.
    gone_ln = next(ln for ln in lines if ln not in (lines[0], other))
    (tree / field(gone_ln, 14)).unlink()
    # And blank a fourth row's lob_file to the empty enclosed field
    # (datfile parses `` to '', not None — round-1 finding D14).
    empty_ln = next(ln for ln in lines
                    if ln not in (lines[0], other, gone_ln))
    fields = empty_ln.split("\t")
    fields[14] = "``"
    lines[lines.index(empty_ln)] = "\t".join(fields)
    dat.write_text("\n".join([*lines, "\t".join(dup)]) + "\n")

    z = _zip(tree, tmp_path / "pubinfo_mini.zip")
    out = tmp_path / "current.db"
    report = build_current_db(z, out, fts=False, analysis_text=False)
    assert "2 bill version lobs absent from zip" in report.warnings

    con = sqlite3.connect(out)
    try:
        with zipfile.ZipFile(z) as zf:
            names = set(zf.namelist())
            rows = con.execute(
                "SELECT bill_version_id, lob_file, title_text"
                " FROM bill_version").fetchall()
            for _vid, lob, title in rows:
                if lob in names:
                    xml = zf.read(lob).decode("utf-8", errors="replace")
                    assert title == caml.extract_title(xml)
                else:
                    assert title is None  # stranded row: no phantom title
        covered = {vid for vid, lob, _t in rows if lob in names}
        n_text, = con.execute(
            "SELECT count(*) FROM bill_version_text").fetchone()
        assert n_text == len(covered)  # one row per distinct covered vid
        stored, = con.execute(
            """SELECT coalesce(sum(length(text_zlib)), 0)
               FROM bill_version_text""").fetchone()
        assert report.version_text_bytes == stored  # no double-count
    finally:
        con.close()


def test_bill_zip_is_the_version_text_source(fixtures, tmp_path):
    """The nightly's real shape: bill tables AND version lobs come from
    the daily bill zip; the law zip may not carry the lobs at all. The
    extraction stage must read from the bill zip — a law-zip fallback
    would strip bill_version_text from every nightly artifact (the
    fail-level gate would block the upload; the suite should fail
    first)."""
    law_tree = tmp_path / "law"
    bill_tree = tmp_path / "bill"
    shutil.copytree(fixtures / "mini", law_tree)
    shutil.copytree(fixtures / "mini", bill_tree)
    for lob in law_tree.glob("BILL_VERSION_TBL_*.lob"):
        lob.unlink()
    out = tmp_path / "current.db"
    report = build_current_db(
        _zip(law_tree, tmp_path / "pubinfo_2025.zip"), out,
        bill_zip=_zip(bill_tree, tmp_path / "pubinfo_daily_Fri.zip"),
        fts=False, analysis_text=False)
    assert not [w for w in report.warnings if "bill version lobs" in w]
    con = sqlite3.connect(out)
    try:
        n_lob, = con.execute("""SELECT count(*) FROM bill_version
                                WHERE lob_file IS NOT NULL""").fetchone()
        n_text, = con.execute(
            "SELECT count(*) FROM bill_version_text").fetchone()
        assert n_lob > 0 and n_text == n_lob == report.title_lobs
    finally:
        con.close()


def test_extraction_crosses_the_chunk_boundary(fixtures, tmp_path):
    """Every committed fixture fits one 400-lob worker chunk, so the
    multi-chunk path — and the += accumulation across chunks — would
    otherwise go unexercised. A per-chunk loss is exactly the sub-1%
    silent damage the gate's tolerance absorbs at production scale."""
    tree = tmp_path / "tree"
    shutil.copytree(fixtures / "mini", tree)
    dat = tree / "BILL_VERSION_TBL.dat"
    lines = [ln for ln in dat.read_text().splitlines() if ln.strip()]
    r0 = lines[0].split("\t")
    vid = r0[0].strip("`")
    clones = []
    for i in range(401):  # 16 real rows + 401 clones = 2 chunks of 400
        c = list(r0)
        c[0] = f"`{vid}C{i:03d}`"
        clones.append("\t".join(c))
    dat.write_text("\n".join(lines + clones) + "\n")
    out = tmp_path / "current.db"
    report = build_current_db(_zip(tree, tmp_path / "z.zip"), out,
                              fts=False, analysis_text=False)
    n = len(lines) + 401
    assert report.title_lobs == n
    con = sqlite3.connect(out)
    try:
        assert con.execute("SELECT count(*) FROM bill_version_text"
                           ).fetchone()[0] == n
        assert con.execute("""SELECT count(*) FROM bill_version
                              WHERE title_text IS NULL""").fetchone()[0] == 0
    finally:
        con.close()


def test_corrupt_lob_is_counted_never_fatal(fixtures, tmp_path):
    """A rotten zip member (bad CRC — real corpora contain them, SPEC §6)
    must surface as the extraction-failure warning and cost exactly that
    one version its text, not abort the build."""
    z = _zip(fixtures / "mini", tmp_path / "z.zip")
    with zipfile.ZipFile(z) as zf:
        victim = min(n for n in zf.namelist()
                     if n.startswith("BILL_VERSION_TBL_"))
        off = zf.getinfo(victim).header_offset
    data = bytearray(z.read_bytes())
    # Local file header: 30 bytes + name + extra, then the (stored)
    # payload — flip one payload byte so the member's CRC check fails.
    name_len = int.from_bytes(data[off + 26:off + 28], "little")
    extra_len = int.from_bytes(data[off + 28:off + 30], "little")
    data[off + 30 + name_len + extra_len + 8] ^= 0xFF
    z.write_bytes(bytes(data))
    out = tmp_path / "current.db"
    report = build_current_db(z, out, fts=False, analysis_text=False)
    assert "1 bill version lobs failed extraction" in report.warnings
    con = sqlite3.connect(out)
    try:
        n_lob, = con.execute("""SELECT count(*) FROM bill_version
                                WHERE lob_file IS NOT NULL""").fetchone()
        assert report.title_lobs == n_lob - 1
        vid, title = con.execute(
            """SELECT bill_version_id, title_text FROM bill_version
               WHERE lob_file=?""", (victim,)).fetchone()
        assert title is None
        assert con.execute("""SELECT count(*) FROM bill_version_text
                              WHERE bill_version_id=?""",
                           (vid,)).fetchone()[0] == 0
    finally:
        con.close()


def test_zero_version_rows_build_is_sound(fixtures, tmp_path):
    """A source with an empty BILL_VERSION_TBL must still build: empty
    todo, no worker output, and the size line coalesces to 0."""
    tree = tmp_path / "tree"
    shutil.copytree(fixtures / "mini", tree)
    (tree / "BILL_VERSION_TBL.dat").write_text("")
    report = build_current_db(_zip(tree, tmp_path / "z.zip"),
                              tmp_path / "current.db",
                              fts=False, analysis_text=False)
    assert report.title_lobs == 0
    assert report.version_text_bytes == 0


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

"""Build current.db from pubinfo zips.

Source-selection semantics (SPEC §3):

- ``law_zip`` — the full session zip (``pubinfo_YYYY.zip``, refreshed
  Sundays). Sole source of LAW_* tables in the normal case; also the
  fallback source of bill tables.
- ``bill_zip`` — optional ``pubinfo_daily_[Day].zip`` (nightly, complete
  bill data, no law tables). When given, all BILL tables and their lobs
  load from it instead — that's what makes bill data daily-fresh.
- ``incremental_zip`` — optional small ``pubinfo_[Day].zip``. Normally
  bill-only deltas (ignored: the daily zip supersedes it), but if it ever
  ships a LAW_* table (expected around January), that table loads from it
  in preference to law_zip. Law tables are TRUNCATE+reload artifacts, so
  "apply" means wholesale per-table replacement — never row merges.

The build is stateless and atomic: everything is written to ``<out>.tmp``
and renamed over ``out`` only after indexes, FTS, and meta are complete.
"""

from __future__ import annotations

import collections
import concurrent.futures
import datetime
import json
import logging
import os
import sqlite3
import time
import zipfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path

from ingest import __version__, analyses, caml, datfile, titles
from ingest.archive import _bill_text_worker
from ingest.normalize import law_section_key
from ingest.tables import ALL_TABLES, LAW_TABLES, Table

log = logging.getLogger("ingest.build")

SCHEMA_VERSION = 1

# Analysis text is stored zlib-compressed (~3-4x): ~20k docx per session
# would otherwise add hundreds of MB of plain text to a nightly artifact.
_ZLIB_LEVEL = 6


@dataclass
class BuildReport:
    out_path: str = ""
    law_source: str = ""
    bill_source: str = ""
    law_tables_from_incremental: list[str] = field(default_factory=list)
    table_rows: dict[str, int] = field(default_factory=dict)
    bad_rows: dict[str, int] = field(default_factory=dict)
    statute_lobs: int = 0
    title_lobs: int = 0
    version_text_bytes: int = 0
    title_coverage: dict[str, int] = field(default_factory=dict)
    refs: int = 0
    refs_missing_current: int = 0
    residue: list[str] = field(default_factory=list)  # capped sample
    analysis_formats: dict[str, int] = field(default_factory=dict)
    analysis_missing_lob: int = 0
    analysis_unconverted_doc: int = 0
    analysis_extract_errors: int = 0
    veto_texts: int = 0
    law_extract_date: str | None = None
    bill_extract_date: str | None = None
    session_year: str | None = None
    db_bytes: int = 0
    seconds: float = 0.0
    warnings: list[str] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(self.__dict__, indent=2)


def build_current_db(
    law_zip: Path,
    out: Path,
    *,
    bill_zip: Path | None = None,
    incremental_zip: Path | None = None,
    fts: bool = True,
    analysis_text: bool = True,
    residue_cap: int = 50,
    workers: int = 4,
) -> BuildReport:
    t0 = time.time()
    report = BuildReport(out_path=str(out))
    tmp = Path(f"{out}.tmp")
    if tmp.exists():
        tmp.unlink()

    try:
        with _open_zips(law_zip, bill_zip, incremental_zip) as (
                zf_law, zf_bill, zf_inc):
            report.law_source = law_zip.name
            report.bill_source = (bill_zip or law_zip).name

            con = sqlite3.connect(tmp)
            con.execute("PRAGMA journal_mode=OFF")
            con.execute("PRAGMA synchronous=OFF")
            try:
                _load_tables(con, report, zf_law, zf_bill, zf_inc)
                _extract_statute_text(con, report, zf_law, zf_inc)
                _extract_bill_text(con, report, zf_bill or zf_law, workers)
                _build_section_refs(con, report, residue_cap)
                if analysis_text:
                    _extract_analysis_text(con, report, zf_bill or zf_law)
                _extract_veto_text(con, report, zf_bill or zf_law)
                _create_indexes(con)
                if fts:
                    _build_fts(con)
                _write_meta(con, report)
                con.commit()
            finally:
                con.close()
    except BaseException:
        # Never strand a partial multi-hundred-MB artifact.
        tmp.unlink(missing_ok=True)
        raise

    os.replace(tmp, out)
    report.db_bytes = os.path.getsize(out)
    report.seconds = round(time.time() - t0, 1)
    log.info("built %s: %.0f MB in %.0fs", out, report.db_bytes / 1e6,
             report.seconds)
    return report


class _open_zips:
    def __init__(self, law: Path, bill: Path | None, inc: Path | None):
        self.paths = (law, bill, inc)
        self.zips: list[zipfile.ZipFile | None] = []

    def __enter__(self):
        try:
            for p in self.paths:
                self.zips.append(zipfile.ZipFile(p) if p else None)
        except BaseException:
            self.__exit__()  # close whatever opened before the failure
            raise
        return tuple(self.zips)

    def __exit__(self, *exc):
        for z in self.zips:
            if z:
                z.close()


def _law_source_for(table: Table, zf_law: zipfile.ZipFile,
                    zf_inc: zipfile.ZipFile | None) -> zipfile.ZipFile:
    """Per-table source choice: the incremental wins if it carries this
    LAW table (TRUNCATE+reload — the newer dump is the whole truth)."""
    if zf_inc and f"{table.dat}.dat" in zf_inc.namelist():
        return zf_inc
    return zf_law


def _load_tables(con, report: BuildReport, zf_law, zf_bill, zf_inc) -> None:
    for table in ALL_TABLES:
        if table in LAW_TABLES:
            src = _law_source_for(table, zf_law, zf_inc)
            if src is zf_inc:
                report.law_tables_from_incremental.append(table.name)
                log.warning("LAW table %s present in incremental zip — "
                            "loading from it", table.dat)
        else:
            src = zf_bill or zf_law
        _load_one(con, src, table, report)


def _load_one(con, zf: zipfile.ZipFile, table: Table,
              report: BuildReport) -> None:
    cols = table.columns
    con.execute(f"CREATE TABLE {table.name}({', '.join(cols)})")
    dat_name = f"{table.dat}.dat"
    if dat_name not in zf.namelist():
        report.table_rows[table.name] = 0
        report.warnings.append(f"{dat_name} not present in {Path(zf.filename).name}")
        log.warning("%s not present in source zip", dat_name)
        return
    rows = datfile.parse_bytes(zf.read(dat_name))
    good = [r for r in rows if len(r) == len(cols)]
    bad = len(rows) - len(good)
    if bad:
        report.bad_rows[table.name] = bad
        report.warnings.append(
            f"{dat_name}: {bad} rows with wrong field count dropped")
        log.warning("%s: %d rows with wrong field count (expected %d)",
                    dat_name, bad, len(cols))
    con.executemany(
        f"INSERT INTO {table.name} VALUES ({','.join('?' * len(cols))})", good)
    report.table_rows[table.name] = len(good)
    log.info("%s: %d rows", table.name, len(good))


def _extract_statute_text(con, report: BuildReport, zf_law, zf_inc) -> None:
    """content_text from law-section lobs; section_num_norm (CONS-aware)."""
    t = time.time()
    con.execute("ALTER TABLE law_section ADD COLUMN content_text")
    con.execute("ALTER TABLE law_section ADD COLUMN section_num_norm")
    # Lobs live in whichever zip the LAW_SECTION table came from — but an
    # incremental may ship the .dat without the full lob set, so fall back
    # to the cached full zip for lobs it lacks.
    src = zf_law
    if "law_section" in report.law_tables_from_incremental:
        src = zf_inc
    names = set(src.namelist())
    law_names = set(zf_law.namelist()) if src is not zf_law else names
    text_updates, norm_updates = [], []
    rows = con.execute("SELECT lob_file, law_code, section_num, article, rowid"
                       " FROM law_section").fetchall()
    for lob, law_code, section_num, article, rowid in rows:
        if lob and lob in names:
            xml = src.read(lob).decode("utf-8", errors="replace")
            text_updates.append((caml.law_section_text(xml), rowid))
        elif lob and lob in law_names:
            xml = zf_law.read(lob).decode("utf-8", errors="replace")
            text_updates.append((caml.law_section_text(xml), rowid))
        norm_updates.append(
            (law_section_key(law_code, section_num, article), rowid))
    con.executemany(
        "UPDATE law_section SET content_text=? WHERE rowid=?", text_updates)
    con.executemany(
        "UPDATE law_section SET section_num_norm=? WHERE rowid=?", norm_updates)
    report.statute_lobs = len(text_updates)
    missing = len(rows) - len(text_updates)
    if missing:
        report.warnings.append(f"{missing} law sections without a lob")
    log.info("statute text: %d lobs in %.0fs", len(text_updates),
             time.time() - t)


def _extract_bill_text(con, report: BuildReport, zf, workers: int) -> None:
    """Title + full flattened text for every bill version with a lob.

    ``title_text`` stays a bill_version column (ref building and tools
    1–7 read it); the flattened body lands zlib-compressed in
    ``bill_version_text``, same shape as archive.db's, so the V2 tools
    read version text identically from either DB (SPEC §11). Reuses the
    archive builder's worker — one code path may not drift from the
    other's extraction or error handling.
    """
    t = time.time()
    con.execute("ALTER TABLE bill_version ADD COLUMN title_text")
    con.execute("""CREATE TABLE bill_version_text(
        bill_version_id PRIMARY KEY, title_text, text_zlib)""")
    names = set(zf.namelist())
    rows = con.execute("SELECT bill_version_id, lob_file, rowid"
                       " FROM bill_version").fetchall()
    vid_of = {rowid: vid for vid, _lob, rowid in rows}
    # Work is keyed by rowid, not bill_version_id (the worker passes the
    # key through opaquely): should a source ever duplicate a version id
    # across different lobs, each row must still be titled from its OWN
    # lob, exactly as the pre-V2 title pass did. `is not None`, not
    # truthiness: build, archive, and the sanity gate must partition on
    # the same predicate, or an empty-string lob_file would sit in the
    # gate's denominator with no build-report account of it.
    todo = [(rowid, lob) for _vid, lob, rowid in rows
            if lob is not None and lob in names]
    missing = sum(1 for _vid, lob, _rowid in rows
                  if lob is not None) - len(todo)
    if missing:
        report.warnings.append(f"{missing} bill version lobs absent from zip")
    chunks = [todo[i:i + 400] for i in range(0, len(todo), 400)]
    errors = 0
    with concurrent.futures.ProcessPoolExecutor(max_workers=workers) as ex:
        for out in ex.map(_bill_text_worker, [zf.filename] * len(chunks),
                          chunks, [True] * len(chunks)):
            ok = [r for r in out if len(r) == 3]
            errors += len(out) - len(ok)
            con.executemany(
                "INSERT OR REPLACE INTO bill_version_text VALUES (?,?,?)",
                [(vid_of[rid], title, z) for rid, title, z in ok])
            con.executemany(
                "UPDATE bill_version SET title_text=? WHERE rowid=?",
                [(title, rid) for rid, title, _z in ok])
            report.title_lobs += len(ok)
    # Measured from the table so duplicate-key re-extraction can never
    # double-count: the size line reports bytes actually stored.
    report.version_text_bytes = con.execute(
        "SELECT coalesce(sum(length(text_zlib)), 0)"
        " FROM bill_version_text").fetchone()[0]
    if errors:
        report.warnings.append(f"{errors} bill version lobs failed extraction")
    log.info("bill text: %d lobs, %.0f MB compressed in %.0fs",
             report.title_lobs, report.version_text_bytes / 1e6,
             time.time() - t)


def _build_section_refs(con, report: BuildReport, residue_cap: int) -> None:
    """Parse every version title into bill_section_ref.

    ``exists_in_current_law`` is precomputed here (SPEC §6): refs to absent
    sections are kept and flagged — they signal repealed targets or
    cross-bill contingencies.
    """
    t = time.time()
    con.execute("""CREATE TABLE bill_section_ref(
        bill_version_id, bill_id, action, law_code, section, is_range,
        range_end, struct, exists_in_current_law)""")
    current = set(con.execute(
        "SELECT law_code, section_num_norm FROM law_section"))
    coverage: collections.Counter[str] = collections.Counter()
    out = []
    missing = 0
    for vid, bid, title in con.execute(
            """SELECT bill_version_id, bill_id, title_text FROM bill_version
               WHERE title_text IS NOT NULL""").fetchall():
        result = titles.parse_title(title)
        coverage[result.status] += 1
        if result.status in ("partial", "fail") and \
                len(report.residue) < residue_cap:
            report.residue.append(f"[{result.status}] {vid}: {title}")
        for r in result.refs:
            exists = int((r.code, r.section) in current)
            if not exists:
                missing += 1
            out.append((vid, bid, r.action, r.code, r.section,
                        int(r.is_range), r.range_end, r.struct, exists))
    con.executemany(
        "INSERT INTO bill_section_ref VALUES (?,?,?,?,?,?,?,?,?)", out)
    report.title_coverage = dict(coverage)
    report.refs = len(out)
    report.refs_missing_current = missing
    log.info("bill_section_ref: %d refs (%d not in current law), "
             "coverage %s in %.0fs",
             len(out), missing, dict(coverage), time.time() - t)


def _extract_analysis_text(con, report: BuildReport, zf) -> None:
    """Extracted analysis text, zlib-compressed, keyed by analysis_id.

    Not in the SPEC §4 current.db table list, but required by tool 5
    (get_bill_analyses returns full text for current-session bills).
    """
    t = time.time()
    con.execute("""CREATE TABLE analysis_text(
        analysis_id PRIMARY KEY, format, text_zlib)""")
    names = set(zf.namelist())
    formats: collections.Counter[str] = collections.Counter()
    rows = con.execute(
        "SELECT analysis_id, lob_file FROM bill_analysis").fetchall()
    doc_batch: list[tuple[str, bytes]] = []
    inserts = []
    for aid, lob in rows:
        if not lob or lob not in names:
            report.analysis_missing_lob += 1
            continue
        data = zf.read(lob)
        # Real corpora contain corrupt lobs (e.g. PK magic but truncated
        # zip). Extraction errors are counted, never fatal (SPEC §6).
        try:
            kind, text = analyses.extract_one(data)
        except Exception as e:  # noqa: BLE001 — any corrupt lob, never fatal
            report.analysis_extract_errors += 1
            if report.analysis_extract_errors <= 20:
                report.warnings.append(
                    f"analysis {aid} ({lob}): extraction failed: {e!r}")
            log.warning("analysis %s (%s): extraction failed: %r",
                        aid, lob, e)
            continue
        formats[kind] += 1
        if kind == "doc":
            # Keyed by analysis_id (the PK, numeric, filesystem-safe):
            # lob-name munging can collide, and one lob shared by two
            # analysis rows must yield text for both.
            doc_batch.append((str(aid), data))
        elif text is None:
            # rtf or other unhandled format: counted, never silent.
            report.analysis_extract_errors += 1
            report.warnings.append(f"analysis {aid}: unhandled format {kind}")
        else:
            inserts.append((aid, kind, zlib.compress(text.encode(), _ZLIB_LEVEL)))
    if doc_batch:
        if analyses.soffice_available():
            for i in range(0, len(doc_batch), 50):
                chunk = doc_batch[i:i + 50]
                try:
                    converted = analyses.extract_doc_batch(chunk)
                except Exception as e:  # noqa: BLE001 — hung/broken soffice
                    report.analysis_extract_errors += len(chunk)
                    report.warnings.append(
                        f".doc batch of {len(chunk)} failed: {e!r}")
                    log.warning(".doc batch failed: %r", e)
                    continue
                for key, text in converted.items():
                    inserts.append((key, "doc",
                                    zlib.compress(text.encode(), _ZLIB_LEVEL)))
        else:
            report.analysis_unconverted_doc = len(doc_batch)
            report.warnings.append(
                f"{len(doc_batch)} legacy .doc analyses skipped: "
                "LibreOffice (soffice) not available")
    con.executemany(
        "INSERT OR REPLACE INTO analysis_text VALUES (?,?,?)", inserts)
    report.analysis_formats = dict(formats)
    log.info("analysis text: %d stored (%s) in %.0fs", len(inserts),
             dict(formats), time.time() - t)


def _extract_veto_text(con, report: BuildReport, zf) -> None:
    """Veto message text (plain-text lobs, 2015+ format) — tool 6 returns
    veto messages for current-session bills too."""
    con.execute("""CREATE TABLE veto_text(
        bill_id, veto_date, format, text_zlib)""")
    names = set(zf.namelist())
    inserts = []
    for bid, vdate, lob in con.execute(
            "SELECT bill_id, veto_date, lob_file FROM veto_message"):
        if not lob or lob not in names:
            continue
        try:
            kind, text = analyses.extract_one(zf.read(lob))
            if text is None:
                raise ValueError(f"unhandled veto format {kind}")
            inserts.append((bid, vdate, kind,
                            zlib.compress(text.encode(), _ZLIB_LEVEL)))
        except Exception as e:  # noqa: BLE001 — count, never fatal
            report.warnings.append(f"veto {bid}: {e!r}")
    con.executemany("INSERT INTO veto_text VALUES (?,?,?,?)", inserts)
    report.veto_texts = len(inserts)
    log.info("veto texts: %d", len(inserts))


def _create_indexes(con) -> None:
    con.execute("CREATE INDEX ix_bill_id ON bill(bill_id)")
    con.execute("CREATE INDEX ix_law ON law_section(law_code, section_num_norm)")
    con.execute("CREATE INDEX ix_chapter ON bill(chapter_year, chapter_num)")
    con.execute("CREATE INDEX ix_measure ON bill(measure_type, measure_num)")
    con.execute("CREATE INDEX ix_ver ON bill_version(bill_id)")
    con.execute("CREATE INDEX ix_authors ON bill_version_authors(bill_version_id)")
    con.execute("CREATE INDEX ix_hist ON bill_history(bill_id)")
    con.execute("CREATE INDEX ix_analysis ON bill_analysis(bill_id)")
    con.execute("CREATE INDEX ix_ref ON bill_section_ref(law_code, section)")
    con.execute("CREATE INDEX ix_ref_bill ON bill_section_ref(bill_id)")
    con.execute("CREATE INDEX ix_toc ON law_toc(law_code, node_treepath)")
    con.execute("CREATE INDEX ix_toc_sec ON law_toc_sections(law_code, section_num)")


def _build_fts(con) -> None:
    t = time.time()
    con.execute("""CREATE VIRTUAL TABLE law_fts USING fts5(content_text,
                   content='law_section', content_rowid='rowid')""")
    con.execute("INSERT INTO law_fts(law_fts) VALUES('rebuild')")
    log.info("FTS build: %.0fs", time.time() - t)


def _write_meta(con, report: BuildReport) -> None:
    report.law_extract_date = con.execute(
        "SELECT max(trans_update) FROM law_section").fetchone()[0]
    report.bill_extract_date = con.execute(
        """SELECT max(m) FROM (
             SELECT max(trans_update) AS m FROM bill
             UNION ALL SELECT max(trans_update) FROM bill_version
             UNION ALL SELECT max(trans_update) FROM bill_history
             UNION ALL SELECT max(trans_update) FROM bill_analysis)"""
    ).fetchone()[0]
    report.session_year = con.execute(
        "SELECT max(session_year) FROM bill").fetchone()[0]
    con.execute("CREATE TABLE meta(key PRIMARY KEY, value)")
    build_utc = datetime.datetime.now(datetime.UTC).strftime(
        "%Y-%m-%d %H:%M:%S")
    meta = {
        "schema_version": str(SCHEMA_VERSION),
        "ingest_version": __version__,
        "build_utc": build_utc,
        "session_year": report.session_year or "",
        "law_extract_date": report.law_extract_date or "",
        "bill_extract_date": report.bill_extract_date or "",
        "law_source": report.law_source,
        "bill_source": report.bill_source,
        "law_tables_from_incremental": json.dumps(
            report.law_tables_from_incremental),
        "title_coverage": json.dumps(report.title_coverage),
        "table_rows": json.dumps(report.table_rows),
    }
    con.executemany("INSERT INTO meta VALUES (?,?)", meta.items())

"""Build archive.db from per-session pubinfo zips (1989 -> last closed
session).

One SQLite file, all sessions, committed one session at a time so an
interrupted multi-hour build resumes where it stopped (``resume=True``
skips sessions recorded in ``meta``). Eras differ in what they carry
(SPIKE_FINDINGS era matrix: pre-1999 chaptered bills only, analyses 1993+,
history/votes 1999+); every absence is tolerated and recorded in the
``session_coverage`` table so MCP tools can state limits honestly instead
of returning silent emptiness (SPEC §5).

Text artifacts (bill version text, analysis text) are zlib-compressed —
stdlib, and consistent with current.db's analysis_text. Heavy work is
parallelized: html analyses via a process pool, legacy .doc via concurrent
LibreOffice instances with distinct user profiles.
"""

from __future__ import annotations

import collections
import concurrent.futures
import datetime
import json
import logging
import re
import sqlite3
import tempfile
import threading
import time
import zipfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path

from ingest import __version__, analyses, caml, datfile, titles
from ingest.tables import ARCHIVE_TABLES, Table

log = logging.getLogger("ingest.archive")

SCHEMA_VERSION = 1
_ZLIB_LEVEL = 6
_DOC_BATCH = 50

_SESSION_ZIP = re.compile(r"pubinfo_(\d{4})\.zip$")


@dataclass
class SessionReport:
    session: str = ""
    table_rows: dict[str, int] = field(default_factory=dict)
    bad_rows: dict[str, int] = field(default_factory=dict)
    version_lobs: int = 0
    version_text_bytes: int = 0
    title_coverage: dict[str, int] = field(default_factory=dict)
    refs: int = 0
    residue: list[str] = field(default_factory=list)
    analysis_formats: dict[str, int] = field(default_factory=dict)
    analysis_missing_lob: int = 0
    analysis_extract_errors: int = 0
    veto_texts: int = 0
    seconds: float = 0.0
    warnings: list[str] = field(default_factory=list)


@dataclass
class ArchiveReport:
    out_path: str = ""
    sessions: list[SessionReport] = field(default_factory=list)
    skipped_sessions: list[str] = field(default_factory=list)
    failed_sessions: list[list[str]] = field(default_factory=list)
    db_bytes: int = 0
    seconds: float = 0.0

    def to_json(self) -> str:
        return json.dumps({
            "out_path": self.out_path,
            "skipped_sessions": self.skipped_sessions,
            "failed_sessions": self.failed_sessions,
            "db_bytes": self.db_bytes,
            "seconds": self.seconds,
            "sessions": [s.__dict__ for s in self.sessions],
        }, indent=2)


def _session_of(zip_path: Path) -> str:
    m = _SESSION_ZIP.search(zip_path.name)
    if not m:
        raise ValueError(f"not a session zip name: {zip_path.name}")
    return m.group(1)


def build_archive_db(
    session_zips: list[Path],
    out: Path,
    *,
    resume: bool = False,
    workers: int = 4,
    bill_text: bool = True,
    analysis_text: bool = True,
    residue_cap: int = 30,
) -> ArchiveReport:
    t0 = time.time()
    report = ArchiveReport(out_path=str(out))
    ordered = sorted(session_zips, key=_session_of)

    if out.exists() and not resume:
        out.unlink()
    con = sqlite3.connect(out)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    try:
        _create_schema(con)
        done = {k.removeprefix("session_done_")
                for (k,) in con.execute(
                    "SELECT key FROM meta WHERE key LIKE 'session_done_%'")}
        for zp in ordered:
            session = _session_of(zp)
            if session in done:
                report.skipped_sessions.append(session)
                log.info("session %s already built — skipped", session)
                continue
            try:
                srep = _build_session(
                    con, zp, session, workers=workers, bill_text=bill_text,
                    analysis_text=analysis_text, residue_cap=residue_cap)
                report.sessions.append(srep)
            except Exception as e:  # noqa: BLE001 — one rotten session zip
                # (truncated download, bad central directory) must not
                # abort the other 17. Roll back its partial rows, record
                # the failure, continue.
                log.error("session %s FAILED: %r — continuing", session, e)
                try:
                    con.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                con.execute(
                    "INSERT INTO session_coverage VALUES (?,?,?)",
                    (session, "build_error", repr(e)))
                con.commit()
                report.failed_sessions.append([session, repr(e)])
        _write_meta(con, report)
        con.commit()
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        con.close()
    report.db_bytes = out.stat().st_size
    report.seconds = round(time.time() - t0, 1)
    log.info("archive: %.1f GB in %.0fs", report.db_bytes / 1e9,
             report.seconds)
    return report


def _create_schema(con) -> None:
    for table in ARCHIVE_TABLES:
        con.execute(f"CREATE TABLE IF NOT EXISTS {table.name}"
                    f"({', '.join(table.columns)})")
    con.execute("""CREATE TABLE IF NOT EXISTS bill_version_text(
        bill_version_id PRIMARY KEY, title_text, text_zlib)""")
    con.execute("""CREATE TABLE IF NOT EXISTS analysis_text(
        analysis_id PRIMARY KEY, format, text_zlib)""")
    con.execute("""CREATE TABLE IF NOT EXISTS veto_text(
        bill_id, veto_date, format, text_zlib)""")
    con.execute("""CREATE TABLE IF NOT EXISTS bill_section_ref(
        session_year, bill_version_id, bill_id, action, law_code, section,
        is_range, range_end, struct)""")
    con.execute("""CREATE TABLE IF NOT EXISTS session_coverage(
        session_year, key, value)""")
    con.execute("CREATE TABLE IF NOT EXISTS meta(key PRIMARY KEY, value)")


def _create_indexes(con) -> None:
    """Deferred to the end of a full build (cheap to re-run on resume)."""
    ix = [
        "CREATE INDEX IF NOT EXISTS ax_chapter ON bill(chapter_year, chapter_num)",
        "CREATE INDEX IF NOT EXISTS ax_measure ON bill(session_year, measure_type, measure_num)",
        "CREATE INDEX IF NOT EXISTS ax_ver ON bill_version(bill_id)",
        "CREATE INDEX IF NOT EXISTS ax_hist ON bill_history(bill_id)",
        "CREATE INDEX IF NOT EXISTS ax_analysis ON bill_analysis(bill_id)",
        "CREATE INDEX IF NOT EXISTS ax_authors ON bill_version_authors(bill_version_id)",
        "CREATE INDEX IF NOT EXISTS ax_ref ON bill_section_ref(law_code, section)",
        "CREATE INDEX IF NOT EXISTS ax_ref_bill ON bill_section_ref(bill_id)",
        "CREATE INDEX IF NOT EXISTS ax_dvote ON bill_detail_vote(bill_id)",
        "CREATE INDEX IF NOT EXISTS ax_svote ON bill_summary_vote(bill_id)",
        "CREATE INDEX IF NOT EXISTS ax_veto ON veto_message(bill_id)",
    ]
    for stmt in ix:
        con.execute(stmt)


def _build_session(con, zip_path: Path, session: str, *, workers: int,
                   bill_text: bool, analysis_text: bool,
                   residue_cap: int) -> SessionReport:
    t0 = time.time()
    srep = SessionReport(session=session)
    log.info("=== session %s (%s) ===", session, zip_path.name)
    with zipfile.ZipFile(zip_path) as zf:
        names = set(zf.namelist())
        con.execute("BEGIN")
        for table in ARCHIVE_TABLES:
            _load_one(con, zf, names, table, srep)
        _extract_bill_versions(con, zf, zip_path, session, srep, workers,
                               store_text=bill_text)
        _build_refs(con, session, srep, residue_cap)
        if analysis_text:
            _extract_analyses(con, zf, session, srep, workers)
        _extract_veto_text(con, zf, names, session, srep)
        _write_coverage(con, session, srep)
        con.execute(
            "INSERT OR REPLACE INTO meta VALUES (?,?)",
            (f"session_done_{session}",
             datetime.datetime.now(datetime.UTC).strftime(
                 "%Y-%m-%d %H:%M:%S")))
        con.commit()
    srep.seconds = round(time.time() - t0, 1)
    log.info("session %s done in %.0fs", session, srep.seconds)
    return srep


def _load_one(con, zf, names: set[str], table: Table,
              srep: SessionReport) -> None:
    dat_name = f"{table.dat}.dat"
    if dat_name not in names:
        srep.table_rows[table.name] = 0
        return
    rows = datfile.parse_bytes(zf.read(dat_name))
    good = [r for r in rows if len(r) == len(table.columns)]
    bad = len(rows) - len(good)
    if bad:
        srep.bad_rows[table.name] = bad
        srep.warnings.append(f"{dat_name}: {bad} malformed rows dropped")
        log.warning("%s %s: %d malformed rows dropped",
                    srep.session, dat_name, bad)
    con.executemany(
        f"INSERT INTO {table.name} VALUES "
        f"({','.join('?' * len(table.columns))})", good)
    srep.table_rows[table.name] = len(good)
    log.info("%s: %d rows", table.name, len(good))


# --- bill version text -----------------------------------------------------

def _bill_text_worker(zip_path: str, items: list[tuple[str, str]],
                      store_text: bool) -> list[tuple]:
    """Runs in a worker process: (bill_version_id, lob) -> extracted rows.

    Workers open the zip themselves — shipping lob names through IPC is
    cheap, shipping gigabytes of XML is not. With store_text=False only
    the title is extracted (refs still need titles).
    """
    out = []
    with zipfile.ZipFile(zip_path) as zf:
        for vid, lob in items:
            try:
                xml = zf.read(lob).decode("utf-8", errors="replace")
                title = caml.extract_title(xml)
                ztext = (zlib.compress(caml.bill_text(xml).encode(),
                                       _ZLIB_LEVEL) if store_text else None)
                out.append((vid, title, ztext))
            except Exception as e:  # noqa: BLE001 — count, never crash
                out.append((vid, None, None, repr(e)))
    return out


def _extract_bill_versions(con, zf, zip_path: Path, session: str,
                           srep: SessionReport, workers: int,
                           store_text: bool = True) -> None:
    t = time.time()
    names = set(zf.namelist())
    todo = [(vid, lob) for vid, lob in con.execute(
        """SELECT bill_version_id, lob_file FROM bill_version
           WHERE bill_id LIKE ? AND lob_file IS NOT NULL""",
        (f"{session}%",)).fetchall() if lob in names]
    missing = con.execute(
        """SELECT count(*) FROM bill_version
           WHERE bill_id LIKE ? AND lob_file IS NOT NULL""",
        (f"{session}%",)).fetchone()[0] - len(todo)
    if missing:
        srep.warnings.append(f"{missing} bill version lobs absent from zip")
    chunks = [todo[i:i + 400] for i in range(0, len(todo), 400)]
    errors = 0
    with concurrent.futures.ProcessPoolExecutor(max_workers=workers) as ex:
        for rows in ex.map(_bill_text_worker,
                           [str(zip_path)] * len(chunks), chunks,
                           [store_text] * len(chunks)):
            ok_rows = [r for r in rows if len(r) == 3]
            errors += len(rows) - len(ok_rows)
            con.executemany(
                "INSERT OR REPLACE INTO bill_version_text VALUES (?,?,?)",
                ok_rows)
            srep.version_text_bytes += sum(len(r[2]) for r in ok_rows
                                           if r[2] is not None)
            srep.version_lobs += len(ok_rows)
    if errors:
        srep.warnings.append(f"{errors} bill version lobs failed extraction")
    log.info("%s bill text: %d lobs, %.0f MB compressed, %.0fs",
             session, srep.version_lobs, srep.version_text_bytes / 1e6,
             time.time() - t)


# --- section refs ----------------------------------------------------------

def _build_refs(con, session: str, srep: SessionReport,
                residue_cap: int) -> None:
    t = time.time()
    coverage: collections.Counter[str] = collections.Counter()
    out = []
    rows = con.execute(
        """SELECT v.bill_version_id, v.bill_id, t.title_text
           FROM bill_version v JOIN bill_version_text t
             ON t.bill_version_id = v.bill_version_id
           WHERE v.bill_id LIKE ?""",
        (f"{session}%",)).fetchall()
    for vid, bid, title in rows:
        if title is None:
            # Extraction yielded no title (truncated/corrupt XML): stays
            # in the coverage denominator so the 99% gate can see it.
            coverage["no_title"] += 1
            if len(srep.residue) < residue_cap:
                srep.residue.append(f"[no_title] {vid}")
            continue
        try:
            result = titles.parse_title(title)
        except Exception as e:  # noqa: BLE001 — archive titles get messy;
            coverage["error"] += 1     # log, count, never crash (SPEC §6)
            if len(srep.residue) < residue_cap:
                srep.residue.append(f"[error {e!r}] {vid}: {title[:160]}")
            continue
        coverage[result.status] += 1
        if result.status in ("partial", "fail") and \
                len(srep.residue) < residue_cap:
            srep.residue.append(f"[{result.status}] {vid}: {title[:160]}")
        for r in result.refs:
            out.append((session, vid, bid, r.action, r.code, r.section,
                        int(r.is_range), r.range_end, r.struct))
    con.executemany(
        "INSERT INTO bill_section_ref VALUES (?,?,?,?,?,?,?,?,?)", out)
    srep.title_coverage = dict(coverage)
    srep.refs = len(out)
    n = sum(coverage.values()) or 1
    classified = (n - coverage["partial"] - coverage["fail"]
                  - coverage["error"] - coverage["no_title"])
    if classified / n < 0.99:
        srep.warnings.append(
            f"title classification {100 * classified / n:.1f}% < 99% "
            f"({dict(coverage)}) — investigate residue")
    log.info("%s refs: %d, coverage %s, %.0fs",
             session, len(out), dict(coverage), time.time() - t)


# --- analyses --------------------------------------------------------------

def _html_worker(zip_path: str, items: list[tuple[str, str]]) -> list[tuple]:
    out = []
    with zipfile.ZipFile(zip_path) as zf:
        for aid, lob in items:
            try:
                text = analyses.extract_html(zf.read(lob))
                out.append((aid, "html",
                            zlib.compress(text.encode(), _ZLIB_LEVEL)))
            except Exception as e:  # noqa: BLE001
                out.append((aid, "html", None, repr(e)))
    return out


def _extract_analyses(con, zf, session: str, srep: SessionReport,
                      workers: int) -> None:
    t = time.time()
    names = set(zf.namelist())
    rows = con.execute(
        """SELECT analysis_id, lob_file FROM bill_analysis
           WHERE bill_id LIKE ?""", (f"{session}%",)).fetchall()
    formats: collections.Counter[str] = collections.Counter()
    inserts: list[tuple] = []
    html_items: list[tuple[str, str]] = []
    doc_items: list[tuple[str, str]] = []  # (aid, lob) — bytes stay in zip
    zip_path = zf.filename
    for aid, lob in rows:
        if not lob or lob not in names:
            srep.analysis_missing_lob += 1
            continue
        try:
            # read inside the try: a bad-CRC zip member must count as an
            # extraction error, not abort the session (SPEC §6)
            data = zf.read(lob)
            kind = analyses.detect(data)
            formats[kind] += 1
            if kind == "docx":
                inserts.append((aid, kind, zlib.compress(
                    analyses.extract_docx(data).encode(), _ZLIB_LEVEL)))
            elif kind == "text":
                inserts.append((aid, kind, zlib.compress(
                    analyses.extract_text(data).encode(), _ZLIB_LEVEL)))
            elif kind == "html":
                html_items.append((aid, lob))
            elif kind == "doc":
                # Names only — a 2009-era session's raw .doc payloads run
                # to gigabytes; conversion workers re-read from the zip.
                doc_items.append((str(aid), lob))
            else:
                srep.analysis_extract_errors += 1
                srep.warnings.append(f"analysis {aid}: unhandled {kind}")
        except Exception as e:  # noqa: BLE001 — corrupt lob, never fatal
            srep.analysis_extract_errors += 1
            if srep.analysis_extract_errors <= 20:
                srep.warnings.append(f"analysis {aid} ({lob}): {e!r}")

    if html_items:
        chunks = [html_items[i:i + 500]
                  for i in range(0, len(html_items), 500)]
        with concurrent.futures.ProcessPoolExecutor(
                max_workers=workers) as ex:
            for out in ex.map(_html_worker,
                              [zip_path] * len(chunks), chunks):
                for row in out:
                    if len(row) == 3:
                        inserts.append(row)
                    else:
                        srep.analysis_extract_errors += 1

    if doc_items:
        if analyses.soffice_available():
            _convert_docs(zip_path, doc_items, inserts, srep, workers)
        else:
            srep.analysis_extract_errors += len(doc_items)
            srep.warnings.append(
                f"{len(doc_items)} .doc analyses skipped: no soffice")

    con.executemany(
        "INSERT OR REPLACE INTO analysis_text VALUES (?,?,?)", inserts)
    srep.analysis_formats = dict(formats)
    log.info("%s analyses: %d stored (%s), %d errors, %.0fs",
             session, len(inserts), dict(formats),
             srep.analysis_extract_errors, time.time() - t)


def _convert_docs(zip_path: str, doc_items: list[tuple[str, str]],
                  inserts: list, srep: SessionReport, workers: int) -> None:
    """Concurrent LibreOffice instances, one profile dir per worker slot.

    Threads (not processes): the work happens in soffice subprocesses;
    slot-numbered profiles keep concurrent instances apart. Each thread
    opens its own zip handle (a shared handle isn't read-concurrent) and
    reads only its batch's payloads, so peak memory stays ~batch-sized.
    """
    batches = [doc_items[i:i + _DOC_BATCH]
               for i in range(0, len(doc_items), _DOC_BATCH)]
    with tempfile.TemporaryDirectory() as profiles:
        def convert(batch):
            # Profile keyed by the ACTUAL thread — batch-index % workers
            # can hand two live threads the same profile when batch
            # durations vary, and soffice instances sharing a profile
            # refuse to run.
            with zipfile.ZipFile(zip_path) as zf:
                payload = [(aid, zf.read(lob)) for aid, lob in batch]
            return analyses.extract_doc_batch(
                payload,
                profile_dir=f"{profiles}/lo{threading.get_ident()}")

        with concurrent.futures.ThreadPoolExecutor(
                max_workers=workers) as ex:
            futures = [ex.submit(convert, b) for b in batches]
            for i, fut in enumerate(futures):
                try:
                    for aid, text in fut.result().items():
                        if text:
                            inserts.append((aid, "doc", zlib.compress(
                                text.encode(), _ZLIB_LEVEL)))
                        else:
                            srep.analysis_extract_errors += 1
                except Exception as e:  # noqa: BLE001 — hung/broken batch
                    srep.analysis_extract_errors += len(batches[i])
                    srep.warnings.append(f".doc batch {i}: {e!r}")


def _extract_veto_text(con, zf, names: set[str], session: str,
                       srep: SessionReport) -> None:
    """Veto message text (SPEC §5 tool 6 returns veto messages).

    Lobs ship 2015+ only (plain text); earlier eras carry the
    veto_message table without lobs — the coverage matrix shows the gap.
    """
    inserts = []
    for bid, vdate, lob in con.execute(
            """SELECT bill_id, veto_date, lob_file FROM veto_message
               WHERE bill_id LIKE ?""", (f"{session}%",)).fetchall():
        if not lob or lob not in names:
            continue
        try:
            kind, text = analyses.extract_one(zf.read(lob))
            if text is None:
                raise ValueError(f"unhandled veto format {kind}")
            inserts.append((bid, vdate, kind,
                            zlib.compress(text.encode(), _ZLIB_LEVEL)))
        except Exception as e:  # noqa: BLE001 — count, never fatal
            srep.warnings.append(f"veto {bid}: {e!r}")
    con.executemany("INSERT INTO veto_text VALUES (?,?,?,?)", inserts)
    srep.veto_texts = len(inserts)
    if inserts:
        log.info("%s veto texts: %d", session, len(inserts))


# --- coverage & meta -------------------------------------------------------

def _write_coverage(con, session: str, srep: SessionReport) -> None:
    con.execute("DELETE FROM session_coverage WHERE session_year=?",
                (session,))
    rows = [(session, f"rows_{t}", str(n))
            for t, n in srep.table_rows.items()]
    rows += [
        (session, "version_text_lobs", str(srep.version_lobs)),
        (session, "title_coverage", json.dumps(srep.title_coverage)),
        (session, "refs", str(srep.refs)),
        (session, "analysis_formats", json.dumps(srep.analysis_formats)),
        (session, "analysis_errors", str(srep.analysis_extract_errors)),
        (session, "analysis_missing_lob", str(srep.analysis_missing_lob)),
        (session, "veto_texts", str(srep.veto_texts)),
        (session, "warnings", json.dumps(srep.warnings[:20])),
    ]
    con.executemany("INSERT INTO session_coverage VALUES (?,?,?)", rows)


def _write_meta(con, report: ArchiveReport) -> None:
    _create_indexes(con)
    sessions = [r[0] for r in con.execute(
        """SELECT DISTINCT replace(key, 'session_done_', '') FROM meta
           WHERE key LIKE 'session_done_%' ORDER BY 1""")]
    meta = {
        "schema_version": str(SCHEMA_VERSION),
        "ingest_version": __version__,
        "build_utc": datetime.datetime.now(datetime.UTC).strftime(
            "%Y-%m-%d %H:%M:%S"),
        "sessions": json.dumps(sessions),
    }
    con.executemany("INSERT OR REPLACE INTO meta VALUES (?,?)", meta.items())

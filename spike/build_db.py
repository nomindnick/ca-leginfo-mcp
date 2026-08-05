"""Build a spike SQLite database from a pubinfo session zip.

Usage: python3 build_db.py <pubinfo_YYYY.zip> <out.db>

Loads law tables (with statute text extracted from XML lobs), bill tables
(with titles extracted from bill version lobs), history, analysis metadata,
and vetoes. Reads lobs directly from the zip — nothing is extracted to disk.
"""

import os
import sqlite3
import sys
import time
import zipfile

import caml
import datfile

# Column lists transcribed from the load scripts in pubinfo_load.zip.
TABLES = {
    "CODES_TBL": ["code", "title"],
    "LAW_SECTION_TBL": [
        "id", "law_code", "section_num", "op_statutes", "op_chapter",
        "op_section", "effective_date", "version_id", "division", "title",
        "part", "chapter", "article", "history", "lob_file", "active_flg",
        "trans_uid", "trans_update",
    ],
    "LAW_TOC_TBL": [
        "law_code", "division", "title", "part", "chapter", "article",
        "heading", "active_flg", "trans_uid", "trans_update", "node_sequence",
        "node_level", "node_position", "node_treepath",
        "contains_law_sections", "history_note", "op_statutes", "op_chapter",
        "op_section",
    ],
    "LAW_TOC_SECTIONS_TBL": [
        "id", "law_code", "node_treepath", "section_num", "section_order",
        "title", "op_statutes", "op_chapter", "op_section", "trans_uid",
        "trans_update", "version_id", "seq_num",
    ],
    "BILL_TBL": [
        "bill_id", "session_year", "session_num", "measure_type",
        "measure_num", "measure_state", "chapter_year", "chapter_type",
        "chapter_session_num", "chapter_num", "latest_bill_version_id",
        "active_flg", "trans_uid", "trans_update", "current_location",
        "current_secondary_loc", "current_house", "current_status",
        "days_31st_in_print",
    ],
    "BILL_VERSION_TBL": [
        "bill_version_id", "bill_id", "version_num", "action_date", "action",
        "request_num", "subject", "vote_required", "appropriation",
        "fiscal_committee", "local_program", "substantive_changes", "urgency",
        "tax_levy", "lob_file", "active_flg", "trans_uid", "trans_update",
    ],
    "BILL_HISTORY_TBL": [
        "bill_id", "bill_history_id", "action_date", "action", "trans_uid",
        "trans_update", "action_sequence", "action_code", "action_status",
        "primary_location", "secondary_location", "ternary_location",
        "end_status",
    ],
    "BILL_ANALYSIS_TBL": [
        "analysis_id", "bill_id", "house", "analysis_type", "committee_code",
        "committee_name", "amendment_author", "analysis_date",
        "amendment_date", "page_num", "lob_file", "released_floor",
        "active_flg", "trans_uid", "trans_update",
    ],
    "VETO_MESSAGE_TBL": ["bill_id", "veto_date", "lob_file", "trans_uid",
                         "trans_update"],
}


def load_table(con, zf, dat_name, table, cols):
    data = zf.read(dat_name)
    rows = datfile.parse_bytes(data)
    bad = [r for r in rows if len(r) != len(cols)]
    if bad:
        print(f"  WARNING {dat_name}: {len(bad)} rows with wrong field count "
              f"(expected {len(cols)}, e.g. {len(bad[0])})")
        rows = [r for r in rows if len(r) == len(cols)]
    con.execute(f"CREATE TABLE {table}({', '.join(cols)})")
    con.executemany(
        f"INSERT INTO {table} VALUES ({','.join('?' * len(cols))})", rows)
    print(f"  {table}: {len(rows)} rows")
    return rows


def main(zip_path, db_path):
    t0 = time.time()
    if os.path.exists(db_path):
        os.remove(db_path)
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA journal_mode=OFF")
    con.execute("PRAGMA synchronous=OFF")

    with zipfile.ZipFile(zip_path) as zf:
        names = set(zf.namelist())
        for dat, cols in TABLES.items():
            if f"{dat}.dat" in names:
                load_table(con, zf, f"{dat}.dat", dat.lower()[:-4], cols)
            else:
                print(f"  {dat}: not present in zip")

        # Statute text from law section lobs.
        if "LAW_SECTION_TBL.dat" in names:
            t = time.time()
            con.execute("ALTER TABLE law_section ADD COLUMN content_text")
            con.execute(
                "ALTER TABLE law_section ADD COLUMN section_num_norm")
            updates = []
            for lob, rowid in con.execute(
                    "SELECT lob_file, rowid FROM law_section").fetchall():
                xml = zf.read(lob).decode("utf-8", errors="replace")
                updates.append((caml.law_section_text(xml), rowid))
            con.executemany(
                "UPDATE law_section SET content_text=? WHERE rowid=?",
                updates)
            con.execute("""UPDATE law_section SET section_num_norm =
                           rtrim(section_num, '.')""")
            print(f"  statute text: {len(updates)} lobs in "
                  f"{time.time() - t:.0f}s")

        # Bill titles from version lobs.
        if "BILL_VERSION_TBL.dat" in names:
            t = time.time()
            con.execute("ALTER TABLE bill_version ADD COLUMN title_text")
            updates = []
            for lob, rowid in con.execute(
                    "SELECT lob_file, rowid FROM bill_version").fetchall():
                if lob and lob in names:
                    xml = zf.read(lob).decode("utf-8", errors="replace")
                    updates.append((caml.extract_title(xml), rowid))
            con.executemany(
                "UPDATE bill_version SET title_text=? WHERE rowid=?",
                updates)
            print(f"  bill titles: {len(updates)} lobs in "
                  f"{time.time() - t:.0f}s")

    con.execute("CREATE INDEX ix_law ON law_section(law_code, "
                "section_num_norm)")
    con.execute("CREATE INDEX ix_chapter ON bill(chapter_year, "
                "chapter_num)")
    con.execute("CREATE INDEX ix_hist ON bill_history(bill_id)")
    con.execute("CREATE INDEX ix_analysis ON bill_analysis(bill_id)")
    con.commit()

    size_mb = os.path.getsize(db_path) / 1e6
    print(f"DB without FTS: {size_mb:.0f} MB")

    t = time.time()
    con.execute("""CREATE VIRTUAL TABLE law_fts USING fts5(content_text,
                   content='law_section', content_rowid='rowid')""")
    con.execute("INSERT INTO law_fts(law_fts) VALUES('rebuild')")
    con.commit()
    con.execute("PRAGMA wal_checkpoint")
    print(f"FTS build: {time.time() - t:.0f}s, "
          f"DB now {os.path.getsize(db_path) / 1e6:.0f} MB")
    con.close()
    print(f"Total build: {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])

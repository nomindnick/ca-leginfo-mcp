"""End-to-end demo of the three MCP features against the spike DB.

Usage: python3 demo.py <spike.db>
"""

import re
import sqlite3
import sys

import titles


def build_refs(con):
    con.execute("DROP TABLE IF EXISTS bill_section_ref")
    con.execute("""CREATE TABLE bill_section_ref(
        bill_version_id, bill_id, action, law_code, section, is_range,
        range_end, struct)""")
    rows = con.execute("""SELECT bill_version_id, bill_id, title_text
                          FROM bill_version WHERE title_text IS NOT NULL""")
    out = []
    for vid, bid, title in rows.fetchall():
        for r in titles.parse_title(title).refs:
            out.append((vid, bid, r.action, r.code, r.section,
                        int(r.is_range), r.range_end, r.struct))
    con.executemany("INSERT INTO bill_section_ref VALUES (?,?,?,?,?,?,?,?)",
                    out)
    con.execute("""CREATE INDEX ix_ref ON bill_section_ref(law_code,
                   section)""")
    con.commit()
    print(f"bill_section_ref: {len(out)} rows\n")


def get_section(con, code, sec):
    print(f"=== get_section({code}, {sec}) ===")
    for r in con.execute(
            """SELECT section_num, effective_date, history,
                      substr(content_text, 1, 150)
               FROM law_section WHERE law_code=? AND section_num_norm=?""",
            (code, sec)):
        print(f"  effective {r[1][:10]}: {r[2]}")
        print(f"  text: {r[3]}...")
    print()


def bills_affecting(con, code, sec):
    print(f"=== bills_affecting_section({code}, {sec}) ===")
    rows = con.execute(
        """SELECT DISTINCT b.measure_type, b.measure_num, r.action,
                  b.current_status, b.current_location, bv.subject
           FROM bill_section_ref r
           JOIN bill b ON b.bill_id = r.bill_id
           JOIN bill_version bv ON bv.bill_version_id =
               b.latest_bill_version_id
           WHERE r.law_code=? AND r.section=?
             AND r.bill_version_id = b.latest_bill_version_id""",
        (code, sec)).fetchall()
    for mt, mn, action, status, loc, subj in rows:
        print(f"  {mt} {mn} [{action}] — {status} ({loc}) — {subj}")
    if not rows:
        print("  (no pending bills reference this section)")
    print()


def history_pivot(con, code, sec):
    print(f"=== legislative_history({code}, {sec}) ===")
    row = con.execute(
        """SELECT history FROM law_section
           WHERE law_code=? AND section_num_norm=? LIMIT 1""",
        (code, sec)).fetchone()
    if not row:
        print("  section not found\n")
        return
    print(f"  history note: {row[0][:120]}")
    m = re.search(r"Stats\.\s+(\d{4}),\s+Ch\.\s+(\d+)", row[0])
    if not m:
        print("  (no Stats. citation in history note)\n")
        return
    year, ch = m.group(1), m.group(2)
    bill = con.execute(
        """SELECT bill_id, measure_type, measure_num FROM bill
           WHERE chapter_year=? AND chapter_num=?""", (year, ch)).fetchone()
    if not bill:
        print(f"  Stats. {year} Ch. {ch}: outside this session DB "
              "(archive lookup in production)\n")
        return
    bid, mt, mn = bill
    print(f"  Stats. {year}, Ch. {ch} = {mt} {mn} ({bid})")
    for r in con.execute(
            """SELECT analysis_date, house, committee_name FROM bill_analysis
               WHERE bill_id=? ORDER BY analysis_date""", (bid,)):
        print(f"    analysis {r[0][:10]} [{r[1]}] {r[2]}")
    veto = con.execute("SELECT veto_date FROM veto_message WHERE bill_id=?",
                       (bid,)).fetchone()
    if veto:
        print(f"    VETOED {veto[0]}")
    print()


if __name__ == "__main__":
    con = sqlite3.connect(sys.argv[1])
    build_refs(con)
    get_section(con, "EDC", "44955")
    bills_affecting(con, "GOV", "54957.5")   # Brown Act records
    bills_affecting(con, "PEN", "1050")      # continuances (2 bills seen)
    # A section amended by a 2025 chapter — full two-hop within this DB.
    code, sec = con.execute(
        """SELECT law_code, section_num_norm FROM law_section
           WHERE history LIKE 'Amended by Stats. 2025%Effective January 1,
 2026%' LIMIT 1""").fetchone() or (None, None)
    row = con.execute(
        """SELECT law_code, section_num_norm FROM law_section
           WHERE history LIKE 'Amended by Stats. 2025, Ch. %' LIMIT 1""").fetchone()
    if row:
        history_pivot(con, row[0], row[1])
    history_pivot(con, "EDC", "44955")

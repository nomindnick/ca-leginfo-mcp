"""Run the title parser over every bill version in the spike DB and report
coverage. Residue (partial/fail titles) is written for inspection.

Usage: python3 coverage.py <spike.db> <residue_out.txt>
"""

import collections
import sqlite3
import sys

import titles


def main(db_path, residue_path):
    con = sqlite3.connect(db_path)
    rows = con.execute(
        """SELECT bill_version_id, bill_id, title_text FROM bill_version
           WHERE title_text IS NOT NULL""").fetchall()

    counts = collections.Counter()
    residue = []
    refs_total = 0
    by_unique_title = {}

    for vid, bill_id, title in rows:
        result = titles.parse_title(title)
        counts[result.status] += 1
        refs_total += len(result.refs)
        by_unique_title.setdefault(title, result.status)
        if result.status in ("partial", "fail"):
            residue.append(f"[{result.status}] {vid} ({result.note})\n"
                           f"  {title}\n")

    n = len(rows)
    print(f"bill versions with titles: {n}")
    for status in ("ok", "no_sections", "budget_act", "uncodified", "partial", "fail"):
        c = counts[status]
        print(f"  {status:12s} {c:6d}  ({100 * c / n:.1f}%)")
    print(f"section refs extracted: {refs_total}")

    uniq = collections.Counter(by_unique_title.values())
    u = len(by_unique_title)
    print(f"unique titles: {u}")
    for status in ("ok", "no_sections", "budget_act", "uncodified", "partial", "fail"):
        c = uniq[status]
        print(f"  {status:12s} {c:6d}  ({100 * c / u:.1f}%)")

    with open(residue_path, "w") as f:
        f.writelines(residue)
    print(f"residue written: {len(residue)} titles -> {residue_path}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])

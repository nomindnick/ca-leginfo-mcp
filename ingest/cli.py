"""Command-line interface for the ingest pipeline.

    ca-leginfo-ingest build --law-zip pubinfo_2025.zip --out current.db \
        [--bill-zip pubinfo_daily_Fri.zip] [--incremental pubinfo_Fri.zip] \
        [--no-fts] [--no-analysis-text] [--report report.json]

    ca-leginfo-ingest sanity current.db [--previous prev.db] [--json]

``build`` writes atomically (tmp + rename). ``sanity`` exits nonzero when
any fail-level check fails — the nightly job chains them so a bad artifact
is never uploaded.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from ingest.build import build_current_db
from ingest.sanity import check_db


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ca-leginfo-ingest")
    sub = parser.add_subparsers(dest="command", required=True)

    b = sub.add_parser("build", help="build current.db from pubinfo zips")
    b.add_argument("--law-zip", type=Path, required=True,
                   help="full session zip (pubinfo_YYYY.zip)")
    b.add_argument("--bill-zip", type=Path,
                   help="nightly daily zip (pubinfo_daily_[Day].zip); "
                        "bill tables load from it when given")
    b.add_argument("--incremental", type=Path,
                   help="small pubinfo_[Day].zip; only consulted for LAW_* "
                        "tables (January reload)")
    b.add_argument("--out", type=Path, required=True)
    b.add_argument("--no-fts", action="store_true")
    b.add_argument("--no-analysis-text", action="store_true")
    b.add_argument("--report", type=Path, help="write build report JSON here")

    s = sub.add_parser("sanity", help="gate a built current.db")
    s.add_argument("db", type=Path)
    s.add_argument("--previous", type=Path,
                   help="previous artifact for non-regression checks")
    s.add_argument("--no-analysis-text", action="store_true",
                   help="don't require the analysis_text table")
    s.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(message)s")

    if args.command == "build":
        for p in (args.law_zip, args.bill_zip, args.incremental):
            if p is not None and not p.exists():
                parser.error(f"not found: {p}")
        report = build_current_db(
            args.law_zip, args.out,
            bill_zip=args.bill_zip,
            incremental_zip=args.incremental,
            fts=not args.no_fts,
            analysis_text=not args.no_analysis_text)
        if args.report:
            args.report.write_text(report.to_json())
        print(report.to_json())
        return 0

    if args.command == "sanity":
        for p in (args.db, args.previous):
            if p is not None and not p.exists():
                parser.error(f"not found: {p}")
        rep = check_db(args.db, previous=args.previous,
                       expect_analysis_text=not args.no_analysis_text)
        if args.json:
            print(rep.to_json())
        else:
            for c in rep.checks:
                mark = "ok " if c.ok else ("FAIL" if c.level == "fail"
                                           else "warn")
                print(f"[{mark}] {c.name}" + (f" — {c.detail}" if c.detail
                                              else ""))
            print(f"\nresult: {'PASS' if rep.ok else 'FAIL'} "
                  f"({len(rep.warnings)} warnings)")
        return 0 if rep.ok else 1

    return 2


if __name__ == "__main__":
    sys.exit(main())

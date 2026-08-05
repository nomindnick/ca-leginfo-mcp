"""Sanity gate for a freshly built current.db (SPEC §3).

The nightly job must not upload a corrupt or regressed artifact: the gate
checks absolute floors, known-section spot checks, internal consistency,
and — when the previous night's DB is available — non-regression of row
counts and extract dates. Checks carry a level: any failed ``fail`` check
blocks the upload; ``warn`` checks are reported but don't block.
"""

from __future__ import annotations

import datetime
import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

# Spot checks chosen for stability: text that has been in these sections
# for decades.
_SPOT_CHECKS = (
    ("EDC", "44955", None),          # teacher layoffs; last amended 1983
    ("PEN", "187", "malice"),        # murder
    ("GOV", "54950", "public"),      # Brown Act preamble
    ("CONS", "Art. I, Sec. 1", "inalienable"),
)

_PREV_TABLES = ("law_section", "bill", "bill_version", "bill_history",
                "bill_analysis", "bill_section_ref")


@dataclass
class Check:
    name: str
    level: str  # "fail" | "warn"
    ok: bool
    detail: str = ""


@dataclass
class SanityReport:
    checks: list[Check] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks if c.level == "fail")

    @property
    def warnings(self) -> list[Check]:
        return [c for c in self.checks if not c.ok and c.level == "warn"]

    def to_json(self) -> str:
        return json.dumps(
            {"ok": self.ok, "checks": [c.__dict__ for c in self.checks]},
            indent=2)


def check_db(db: Path, previous: Path | None = None,
             expect_analysis_text: bool = True,
             now: datetime.datetime | None = None) -> SanityReport:
    rep = SanityReport()
    add = rep.checks.append
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        tables = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view')")}

        def count(table: str) -> int:
            return con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]

        # -- meta ------------------------------------------------------
        meta: dict[str, str] = {}
        if "meta" in tables:
            meta = dict(con.execute("SELECT key, value FROM meta"))
        required = ("schema_version", "build_utc", "law_extract_date",
                    "bill_extract_date", "session_year", "title_coverage")
        missing = [k for k in required if not meta.get(k)]
        add(Check("meta present", "fail", not missing,
                  f"missing: {missing}" if missing else ""))

        # -- absolute floors ------------------------------------------
        floors = {"law_section": 160_000, "bill": 1_000,
                  "bill_version": 1_000, "bill_history": 10_000,
                  "bill_analysis": 1_000, "bill_section_ref": 20_000,
                  "bill_version_authors": 1_000}
        counts: dict[str, int] = {}
        for table, floor in floors.items():
            n = counts[table] = count(table) if table in tables else -1
            add(Check(f"{table} >= {floor}", "fail", n >= floor, f"{n} rows"))
        n_codes = count("codes") if "codes" in tables else -1
        add(Check("codes table 25..35 rows", "fail", 25 <= n_codes <= 35,
                  f"{n_codes} rows"))

        # -- law content ----------------------------------------------
        if counts.get("law_section", -1) > 0:
            n = counts["law_section"]
            with_text = con.execute(
                """SELECT count(*) FROM law_section
                   WHERE content_text IS NOT NULL
                     AND length(content_text) > 0""").fetchone()[0]
            add(Check("statute text coverage >= 99.9%", "fail",
                      with_text >= 0.999 * n, f"{with_text}/{n}"))
            no_norm = con.execute(
                """SELECT count(*) FROM law_section
                   WHERE section_num_norm IS NULL""").fetchone()[0]
            add(Check("section_num_norm complete", "fail", no_norm == 0,
                      f"{no_norm} null"))
            cons = con.execute(
                """SELECT count(*) FROM law_section WHERE law_code='CONS'
                   AND section_num_norm LIKE 'Art. %, Sec. %'""").fetchone()[0]
            add(Check("CONS sections use article-scoped keys", "fail",
                      cons >= 300, f"{cons} rows"))
            multi = con.execute(
                """SELECT count(*) FROM (
                     SELECT 1 FROM law_section
                     GROUP BY law_code, section_num_norm
                     HAVING count(*) > 1)""").fetchone()[0]
            add(Check("multi-version sections >= 400", "warn", multi >= 400,
                      f"{multi} sections"))

        # -- spot checks ----------------------------------------------
        if "law_section" in tables:
            for code, sec, needle in _SPOT_CHECKS:
                row = con.execute(
                    """SELECT content_text FROM law_section
                       WHERE law_code=? AND section_num_norm=?
                       ORDER BY length(content_text) DESC LIMIT 1""",
                    (code, sec)).fetchone()
                ok = bool(row and row[0] and len(row[0]) > 100
                          and (needle is None or needle in row[0].lower()))
                add(Check(f"spot: {code} {sec}", "fail", ok,
                          "" if ok else "missing or unexpected text"))

        # -- ref-side CONS keys (title parser regression guard) --------
        if "bill_section_ref" in tables:
            n_cons, n_keyed = con.execute(
                """SELECT count(*), sum(section LIKE 'Art. %, Sec. %')
                   FROM bill_section_ref WHERE law_code='CONS'
                     AND struct IS NULL""").fetchone()
            add(Check("CONS refs use article-scoped keys", "warn",
                      n_cons == 0 or (n_keyed or 0) >= 0.9 * n_cons,
                      f"{n_keyed or 0}/{n_cons}"))

        # -- bill data consistency ------------------------------------
        if counts.get("bill", -1) > 0 and counts.get("bill_version", -1) > 0:
            add(Check("bill_version >= bill", "fail",
                      counts["bill_version"] >= counts["bill"],
                      f"{counts['bill_version']} vs {counts['bill']}"))
            with_lob = con.execute(
                """SELECT count(*) FROM bill_version
                   WHERE lob_file IS NOT NULL""").fetchone()[0]
            titled = con.execute(
                """SELECT count(*) FROM bill_version
                   WHERE lob_file IS NOT NULL AND title_text IS NOT NULL
                     AND length(title_text) > 0""").fetchone()[0]
            add(Check("title extraction >= 99%", "warn",
                      with_lob == 0 or titled >= 0.99 * with_lob,
                      f"{titled}/{with_lob}"))

        # -- title parse coverage -------------------------------------
        if meta.get("title_coverage"):
            cov = json.loads(meta["title_coverage"])
            total = sum(cov.values()) or 1
            bad = cov.get("partial", 0) + cov.get("fail", 0)
            add(Check("title parse fail+partial <= 1%", "fail",
                      bad <= 0.01 * total, f"{bad}/{total} ({cov})"))
            add(Check("title parse ok >= 80%", "warn",
                      cov.get("ok", 0) >= 0.80 * total,
                      f"{cov.get('ok', 0)}/{total}"))

        # -- analysis text --------------------------------------------
        if expect_analysis_text:
            if "analysis_text" not in tables:
                add(Check("analysis_text table present", "fail", False))
            else:
                n_meta = counts.get("bill_analysis", 0)
                n_text = count("analysis_text")
                add(Check("analysis text >= 95% of analyses", "warn",
                          n_meta == 0 or n_text >= 0.95 * n_meta,
                          f"{n_text}/{n_meta}"))

        # -- FTS ------------------------------------------------------
        if "law_fts" not in tables:
            add(Check("law_fts present", "fail", False))
        else:
            hits = con.execute(
                """SELECT count(*) FROM law_fts
                   WHERE law_fts MATCH 'malice aforethought'""").fetchone()[0]
            add(Check("FTS query returns hits", "fail", hits >= 1,
                      f"{hits} hits"))
            n_fts = count("law_fts")
            add(Check("FTS row count == law_section", "fail",
                      n_fts == counts.get("law_section"),
                      f"{n_fts} vs {counts.get('law_section')}"))

        # -- freshness (data timestamps, not build time) --------------
        now = now or datetime.datetime.now(datetime.UTC)
        for key, days in (("law_extract_date", 40), ("bill_extract_date", 21)):
            val = meta.get(key)
            ok, detail = False, "missing"
            if val:
                try:
                    dt = datetime.datetime.strptime(
                        val, "%Y-%m-%d %H:%M:%S").replace(
                            tzinfo=datetime.UTC)
                    age = (now - dt).days
                    ok, detail = age <= days, f"{val} ({age}d old)"
                except ValueError:
                    detail = f"unparseable: {val}"
            add(Check(f"{key} within {days}d", "warn", ok, detail))

        # -- non-regression vs previous artifact ----------------------
        # 98% (not strict >=): sections are legitimately repealed and
        # inactive bill rows pruned; a small shrink is normal, a large one
        # is a broken source file.
        if previous is not None:
            try:
                prev = sqlite3.connect(f"file:{previous}?mode=ro", uri=True)
                prev.execute("SELECT 1 FROM sqlite_master LIMIT 1")
            except sqlite3.Error as e:
                add(Check("previous db readable", "fail", False, str(e)))
                prev = None
            if prev is not None:
                try:
                    prev_tables = {r[0] for r in prev.execute(
                        """SELECT name FROM sqlite_master
                           WHERE type IN ('table','view')""")}
                    for table in _PREV_TABLES:
                        if table not in prev_tables:
                            add(Check(f"{table} >= 98% of previous", "warn",
                                      False, "table missing in previous"))
                            continue
                        p = prev.execute(
                            f"SELECT count(*) FROM {table}").fetchone()[0]
                        n = counts.get(table) if table in counts else (
                            count(table) if table in tables else -1)
                        add(Check(f"{table} >= 98% of previous", "fail",
                                  n >= 0.98 * p, f"{n} vs {p}"))
                    prev_meta = (dict(prev.execute(
                        "SELECT key, value FROM meta"))
                        if "meta" in prev_tables else {})
                    for key in ("law_extract_date", "bill_extract_date"):
                        a = meta.get(key, "")
                        b = prev_meta.get(key, "")
                        add(Check(f"{key} not older than previous", "fail",
                                  a >= b, f"{a} vs {b or '(missing)'}"))
                finally:
                    prev.close()
    finally:
        con.close()
    return rep

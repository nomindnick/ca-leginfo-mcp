"""ingest/sanity.py check_db() against a real mini build.

The mini DB (built from tests/fixtures/mini/) is far below production
floors BY DESIGN: the point of these tests is that the gate catches a
too-small artifact while the content-quality checks (spot checks, text
coverage, key normalization) still pass on the real records it contains.
"""

import datetime
import shutil
import sqlite3
from pathlib import Path

import pytest

from ingest.build import build_current_db
from ingest.sanity import check_db

UTC = datetime.UTC


@pytest.fixture(scope="module")
def mini_db(mini_zip, tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("sanity_db") / "current.db"
    build_current_db(mini_zip, out)
    return out


@pytest.fixture(scope="module")
def bare_db(mini_zip, tmp_path_factory) -> Path:
    """A build without FTS and without analysis text extraction."""
    out = tmp_path_factory.mktemp("sanity_bare") / "bare.db"
    build_current_db(mini_zip, out, fts=False, analysis_text=False)
    return out


@pytest.fixture(scope="module")
def base_report(mini_db):
    return check_db(mini_db)


def by_name(report) -> dict:
    names = [c.name for c in report.checks]
    assert len(names) == len(set(names)), f"duplicate check names: {names}"
    return {c.name: c for c in report.checks}


def _meta_dates(db: Path) -> tuple[datetime.datetime, datetime.datetime]:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        meta = dict(con.execute("SELECT key, value FROM meta"))
    finally:
        con.close()
    parse = lambda v: datetime.datetime.strptime(
        v, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
    return parse(meta["law_extract_date"]), parse(meta["bill_extract_date"])


# -- absolute floors ------------------------------------------------------

def test_gate_fails_on_mini_floors(base_report):
    checks = by_name(base_report)
    floor = checks["law_section >= 160000"]
    assert floor.level == "fail" and not floor.ok
    assert base_report.ok is False


def test_all_bill_floors_fail_too(base_report):
    checks = by_name(base_report)
    for name in ("bill >= 1000", "bill_version >= 1000",
                 "bill_version_text >= 1000", "bill_history >= 10000",
                 "bill_analysis >= 1000", "bill_section_ref >= 20000",
                 "bill_version_authors >= 1000"):
        assert not checks[name].ok, name


def test_codes_floor_passes(base_report):
    # The mini fixture carries the full 30-row codes table.
    assert checks_ok(base_report, "codes table 25..35 rows")


def checks_ok(report, name: str) -> bool:
    return by_name(report)[name].ok


# -- content checks pass on real records ----------------------------------

def test_spot_checks_pass(base_report):
    checks = by_name(base_report)
    for name in ("spot: EDC 44955", "spot: PEN 187", "spot: GOV 54950",
                 "spot: CONS Art. I, Sec. 1"):
        assert checks[name].ok, f"{name}: {checks[name].detail}"


def test_statute_text_coverage_passes(base_report):
    checks = by_name(base_report)
    assert checks["statute text coverage >= 99.9%"].ok
    assert checks["section_num_norm complete"].ok


def test_cons_key_check_fails_only_on_count(base_report, mini_db):
    # The check requires >= 300 CONS rows; mini has only a handful — but
    # every one of them must already use the article-scoped key form, so
    # the failure is purely about count, not key shape.
    check = by_name(base_report)["CONS sections use article-scoped keys"]
    assert check.level == "fail" and not check.ok
    con = sqlite3.connect(f"file:{mini_db}?mode=ro", uri=True)
    try:
        total, keyed = con.execute(
            """SELECT count(*),
                      sum(section_num_norm LIKE 'Art. %, Sec. %')
               FROM law_section WHERE law_code='CONS'""").fetchone()
    finally:
        con.close()
    assert total > 0 and keyed == total


def test_fts_checks_pass(base_report):
    checks = by_name(base_report)
    assert checks["FTS query returns hits"].ok
    assert checks["FTS row count == law_section"].ok
    assert "law_fts present" not in checks  # only reported when missing


def test_meta_present(base_report):
    assert checks_ok(base_report, "meta present")


# -- previous-DB non-regression -------------------------------------------

PREV_COUNT_CHECKS = tuple(
    f"{t} >= 98% of previous"
    for t in ("law_section", "bill", "bill_version", "bill_version_text",
              "bill_history", "bill_analysis", "bill_section_ref"))
PREV_DATE_CHECKS = ("law_extract_date not older than previous",
                    "bill_extract_date not older than previous")


def test_previous_identical_passes(mini_db, tmp_path):
    prev = tmp_path / "previous.db"
    shutil.copy(mini_db, prev)
    checks = by_name(check_db(mini_db, previous=prev))
    for name in PREV_COUNT_CHECKS + PREV_DATE_CHECKS:
        assert checks[name].ok, f"{name}: {checks[name].detail}"


def test_previous_inflated_fails_regression(mini_db, tmp_path):
    prev = tmp_path / "previous.db"
    shutil.copy(mini_db, prev)
    con = sqlite3.connect(prev)
    try:
        # Double the row counts of two tables: the new DB now has < 98%
        # of the "previous" artifact's rows.
        con.execute("INSERT INTO law_section SELECT * FROM law_section")
        con.execute("INSERT INTO bill SELECT * FROM bill")
        # And pretend the previous artifact had fresher law data.
        con.execute("""UPDATE meta SET value='2099-01-01 00:00:00'
                       WHERE key='law_extract_date'""")
        con.commit()
    finally:
        con.close()

    checks = by_name(check_db(mini_db, previous=prev))
    assert not checks["law_section >= 98% of previous"].ok
    assert not checks["bill >= 98% of previous"].ok
    assert not checks["law_extract_date not older than previous"].ok
    # Untouched tables and dates still pass.
    assert checks["bill_history >= 98% of previous"].ok
    assert checks["bill_extract_date not older than previous"].ok


def test_no_previous_means_no_regression_checks(base_report):
    checks = by_name(base_report)
    for name in PREV_COUNT_CHECKS + PREV_DATE_CHECKS:
        assert name not in checks


# -- bill version text (V2, SPEC §11) -------------------------------------

VERSION_TEXT_CHECK = "version text >= 99% of versions with lobs"


def test_version_text_coverage_passes(base_report):
    check = by_name(base_report)[VERSION_TEXT_CHECK]
    assert check.level == "fail" and check.ok, check.detail


def test_gutted_version_text_trips_the_gate(mini_db, tmp_path):
    """Rows present but text_zlib silently lost (the failure the check
    exists for — tools 8–10 would serve nothing) must block upload."""
    db = tmp_path / "gutted.db"
    shutil.copy(mini_db, db)
    con = sqlite3.connect(db)
    try:
        con.execute("UPDATE bill_version_text SET text_zlib=NULL")
        con.commit()
    finally:
        con.close()
    check = by_name(check_db(db))[VERSION_TEXT_CHECK]
    assert check.level == "fail" and not check.ok


def test_v1_artifact_without_version_text_table(mini_db, tmp_path):
    """A pre-V2 artifact trips the gate via the floor check; the
    coverage check must skip, not crash, on the missing table."""
    db = tmp_path / "v1.db"
    shutil.copy(mini_db, db)
    con = sqlite3.connect(db)
    try:
        con.execute("DROP TABLE bill_version_text")
        con.commit()
    finally:
        con.close()
    checks = by_name(check_db(db))
    floor = checks["bill_version_text >= 1000"]
    assert floor.level == "fail" and not floor.ok
    assert VERSION_TEXT_CHECK not in checks


# -- freshness (now= parameter) -------------------------------------------

def test_freshness_fresh(mini_db):
    _law_dt, bill_dt = _meta_dates(mini_db)
    # Just after the bill extract: bill age ~1d; law age is <= that
    # (negative if the law extract is later), so both are in-window.
    checks = by_name(check_db(mini_db, now=bill_dt + datetime.timedelta(days=1)))
    law = checks["law_extract_date within 40d"]
    bill = checks["bill_extract_date within 21d"]
    assert law.level == "warn" and law.ok, law.detail
    assert bill.level == "warn" and bill.ok, bill.detail


def test_freshness_stale(mini_db):
    _, bill_dt = _meta_dates(mini_db)
    checks = by_name(check_db(mini_db,
                              now=bill_dt + datetime.timedelta(days=400)))
    assert not checks["law_extract_date within 40d"].ok
    assert not checks["bill_extract_date within 21d"].ok


def test_freshness_is_warn_level_only(mini_db, base_report):
    # Stale data alone never flips the gate: freshness is warn-level.
    _, bill_dt = _meta_dates(mini_db)
    stale = check_db(mini_db, now=bill_dt + datetime.timedelta(days=400))
    fail_names = {c.name for c in stale.checks if c.level == "fail" and not c.ok}
    assert "law_extract_date within 40d" not in fail_names
    assert "bill_extract_date within 21d" not in fail_names


# -- expect_analysis_text -------------------------------------------------

ANALYSIS_CHECKS = ("analysis_text table present",
                   "analysis text >= 95% of analyses")


def test_analysis_text_checked_by_default(base_report):
    checks = by_name(base_report)
    assert checks["analysis text >= 95% of analyses"].ok
    assert "analysis_text table present" not in checks  # table exists


def test_expect_analysis_text_false_skips_checks(mini_db):
    checks = by_name(check_db(mini_db, expect_analysis_text=False))
    for name in ANALYSIS_CHECKS:
        assert name not in checks


def test_missing_analysis_table_fails_unless_relaxed(bare_db):
    strict = by_name(check_db(bare_db))
    missing = strict["analysis_text table present"]
    assert missing.level == "fail" and not missing.ok
    # bare_db also skipped FTS: that failure is independent of relaxation.
    assert not strict["law_fts present"].ok

    relaxed = by_name(check_db(bare_db, expect_analysis_text=False))
    for name in ANALYSIS_CHECKS:
        assert name not in relaxed
    assert not relaxed["law_fts present"].ok

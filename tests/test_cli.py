"""ingest/cli.py main(): build + sanity subcommands over the real mini zip,
including the incremental LAW-table overlay (January-reload path)."""

import json
import sqlite3
import zipfile
from pathlib import Path

import pytest

from ingest import cli
from ingest.build import BuildReport
from ingest.cli import main

# Field offsets in LAW_SECTION_TBL.dat (see ingest.tables LAW_SECTION_TBL).
_HISTORY_COL = 13
_LOB_COL = 14
_NUM_COLS = 18

_MARKER = "ALTERED HISTORY - incremental overlay test"


@pytest.fixture(scope="module")
def default_build(mini_zip, tmp_path_factory):
    """One full-default CLI build (FTS + analysis text) shared by tests."""
    out = tmp_path_factory.mktemp("cli_db") / "current.db"
    rc = main(["build", "--law-zip", str(mini_zip), "--out", str(out)])
    return rc, out


def _tables(db: Path) -> set:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
    finally:
        con.close()


# -- build ----------------------------------------------------------------

def test_build_defaults(default_build):
    rc, out = default_build
    assert rc == 0
    assert out.exists()
    assert not out.with_name(out.name + ".tmp").exists()
    tables = _tables(out)
    assert "law_fts" in tables and "analysis_text" in tables


def test_build_no_fts_no_analysis_writes_report(mini_zip, tmp_path, capsys):
    out = tmp_path / "fast.db"
    report_path = tmp_path / "report.json"
    capsys.readouterr()  # drain anything from fixture setup
    rc = main(["build", "--law-zip", str(mini_zip), "--out", str(out),
               "--no-fts", "--no-analysis-text", "--workers", "2",
               "--report", str(report_path)])
    assert rc == 0
    assert out.exists()

    # --report wrote JSON, and the same JSON went to stdout.
    report = json.loads(report_path.read_text())
    assert report["table_rows"]["law_section"] > 0
    assert report["law_tables_from_incremental"] == []
    assert report["version_text_bytes"] > 0  # the §11 size line, plumbed
    stdout_report = json.loads(capsys.readouterr().out)
    assert stdout_report["table_rows"] == report["table_rows"]

    tables = _tables(out)
    assert "law_fts" not in tables and "analysis_text" not in tables


def test_workers_flag_reaches_the_builder(mini_zip, tmp_path, monkeypatch):
    """--workers must actually reach build_current_db: argv acceptance
    plus artifact assertions can't prove the plumbing, since a dropped
    kwarg builds identically at the default."""
    seen = {}

    def spy(*args, **kwargs):
        seen.update(kwargs)
        return BuildReport()

    monkeypatch.setattr(cli, "build_current_db", spy)
    rc = main(["build", "--law-zip", str(mini_zip),
               "--out", str(tmp_path / "w.db"), "--workers", "3"])
    assert rc == 0
    assert seen["workers"] == 3


# -- sanity ---------------------------------------------------------------

def test_sanity_on_mini_db_exits_1(default_build, capsys):
    rc, db = default_build
    assert rc == 0
    capsys.readouterr()
    assert main(["sanity", str(db)]) == 1  # floors fail on the mini DB
    assert "result: FAIL" in capsys.readouterr().out


def test_sanity_json_output(default_build, capsys):
    _, db = default_build
    capsys.readouterr()
    rc = main(["sanity", str(db), "--json"])
    assert rc == 1
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is False
    by_name = {c["name"]: c for c in data["checks"]}
    assert by_name["law_section >= 160000"]["ok"] is False
    assert by_name["spot: PEN 187"]["ok"] is True


# -- argparse errors ------------------------------------------------------

def test_build_missing_law_zip_is_usage_error(tmp_path):
    with pytest.raises(SystemExit) as exc:
        main(["build", "--law-zip", str(tmp_path / "nope.zip"),
              "--out", str(tmp_path / "out.db")])
    assert exc.value.code == 2


def test_build_missing_incremental_is_usage_error(mini_zip, tmp_path):
    with pytest.raises(SystemExit) as exc:
        main(["build", "--law-zip", str(mini_zip),
              "--incremental", str(tmp_path / "nope.zip"),
              "--out", str(tmp_path / "out.db")])
    assert exc.value.code == 2


def test_sanity_missing_db_is_usage_error(tmp_path):
    with pytest.raises(SystemExit) as exc:
        main(["sanity", str(tmp_path / "nope.db")])
    assert exc.value.code == 2


def test_no_subcommand_is_usage_error():
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 2


# -- incremental overlay --------------------------------------------------

def _make_incremental(mini_zip: Path, tmp_path: Path) -> Path:
    """A tiny pubinfo_[Day].zip: the mini LAW_SECTION_TBL.dat with EDC
    44955's history column visibly altered, plus that section's lob."""
    with zipfile.ZipFile(mini_zip) as zf:
        dat = zf.read("LAW_SECTION_TBL.dat").decode("utf-8")
        lines = dat.split("\n")
        hits = [i for i, line in enumerate(lines) if "`EDC44955." in line]
        assert len(hits) == 1
        fields = lines[hits[0]].split("\t")
        assert len(fields) == _NUM_COLS
        fields[_HISTORY_COL] = f"`{_MARKER}`"
        lines[hits[0]] = "\t".join(fields)
        lob_name = fields[_LOB_COL]  # bare (unenclosed) field
        lob_data = zf.read(lob_name)

    inc = tmp_path / "pubinfo_Fri.zip"
    with zipfile.ZipFile(inc, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("LAW_SECTION_TBL.dat", "\n".join(lines))
        zf.writestr(lob_name, lob_data)
    return inc


def test_incremental_law_overlay_wins(mini_zip, tmp_path):
    inc = _make_incremental(mini_zip, tmp_path)
    out = tmp_path / "overlay.db"
    report_path = tmp_path / "report.json"
    rc = main(["build", "--law-zip", str(mini_zip),
               "--incremental", str(inc), "--out", str(out),
               "--no-fts", "--no-analysis-text",
               "--report", str(report_path)])
    assert rc == 0

    report = json.loads(report_path.read_text())
    assert report["law_tables_from_incremental"] == ["law_section"]

    con = sqlite3.connect(f"file:{out}?mode=ro", uri=True)
    try:
        history, text = con.execute(
            """SELECT history, content_text FROM law_section
               WHERE law_code='EDC' AND section_num_norm='44955'""").fetchone()
        # The incremental's row replaced the law-zip's wholesale.
        assert history == _MARKER
        # Its lob was read from the incremental zip too.
        assert text and "certificated employees" in text
        # Only one section appears in the altered dat exactly once; the
        # table still has the full mini row count (wholesale replacement,
        # not a merge of a subset).
        n, = con.execute("SELECT count(*) FROM law_section").fetchone()
        assert n == report["table_rows"]["law_section"] > 40
        # LAW tables absent from the incremental still load from law_zip.
        codes, = con.execute("SELECT count(*) FROM codes").fetchone()
        assert codes == 30
    finally:
        con.close()

"""Unit tests for ingest/datfile.py — the pubinfo .dat parser.

Conventions under test (MySQL LOAD DATA, per the pubinfo load scripts):
tab-delimited fields, optional backtick enclosure, backslash escapes,
bare NULL / \\N is SQL NULL, enclosed `NULL` is the literal string, and a
backtick only closes a field when followed by tab / newline / EOF.

Real-data fixtures live in tests/fixtures/datfiles/ and are byte-exact
slices of production pubinfo .dat files.
"""

from pathlib import Path

import pytest

from ingest.datfile import parse_bytes, parse_text

LAW_SECTION_NCOLS = 18


@pytest.fixture(scope="module")
def datfiles(fixtures: Path) -> Path:
    return fixtures / "datfiles"


@pytest.fixture(scope="module")
def slice_rows(datfiles: Path):
    return parse_bytes((datfiles / "law_section_slice.dat").read_bytes())


@pytest.fixture(scope="module")
def cons_rows(datfiles: Path):
    return parse_bytes((datfiles / "law_section_cons.dat").read_bytes())


@pytest.fixture(scope="module")
def escaped_rows(datfiles: Path):
    return parse_bytes((datfiles / "escaped_rows.dat").read_bytes())


# ---------------------------------------------------------------------------
# law_section_slice.dat — 30 real LAW_SECTION_TBL rows, no escapes (fast path)
# ---------------------------------------------------------------------------


class TestLawSectionSlice:
    def test_row_and_column_counts(self, slice_rows):
        assert len(slice_rows) == 30
        assert {len(r) for r in slice_rows} == {LAW_SECTION_NCOLS}

    def test_first_row_known_values(self, slice_rows):
        row = slice_rows[0]
        assert row[0] == "PCC6952.20127671"          # primary key
        assert row[1] == "PCC"                        # law_code
        assert row[2] == "6952."                      # section_num
        assert row[3] == "2012"                       # op_statues (year)
        assert row[4] == "767"                        # op_chapter
        assert row[5] == "1"                          # op_section
        # Bare (unenclosed) non-NULL fields pass through as strings.
        assert row[6] == "2013-01-01 00:00:00"        # effective_date, bare
        assert row[7] == "id_cf6b54a1-4ac0-11e2-9195-94e237791b6c"
        assert row[13].startswith("Added by Stats. 2012, Ch. 767, Sec. 1.")
        assert row[14] == "LAW_SECTION_TBL_1.lob"     # content_xml, bare
        assert row[15] == "Y"
        assert row[16] == "LEG_ESI"
        assert row[17] == "2026-07-17 01:02:40"

    def test_first_row_nulls(self, slice_rows):
        # Bare NULL tokens become SQL NULL (None); exactly title/chapter here.
        row = slice_rows[0]
        assert [i for i, v in enumerate(row) if v is None] == [9, 12]

    def test_fully_populated_hierarchy_row(self, slice_rows):
        # EDC 16194 carries every division/title/part/chapter/article value.
        row = slice_rows[2]
        assert row[0] == "EDC16194.19962772"
        assert row[1] == "EDC"
        assert row[8:13] == ["1.", "1.", "10.", "6.", "3."]
        assert None not in row

    def test_law_codes_and_last_row(self, slice_rows):
        assert {r[1] for r in slice_rows} == {
            "EDC", "FAC", "FAM", "FGC", "HSC", "LAB", "PCC", "PEN", "RTC",
        }
        assert slice_rows[-1][0] == "RTC40063.200245917"
        assert slice_rows[-1][1] == "RTC"
        assert slice_rows[-1][2] == "40063."


# ---------------------------------------------------------------------------
# law_section_cons.dat — Constitution rows (heavy NULL usage, roman articles)
# ---------------------------------------------------------------------------


class TestLawSectionCons:
    def test_row_and_column_counts(self, cons_rows):
        assert len(cons_rows) == 10
        assert {len(r) for r in cons_rows} == {LAW_SECTION_NCOLS}

    def test_all_rows_are_cons(self, cons_rows):
        assert all(r[1] == "CONS" for r in cons_rows)
        assert all(r[0].startswith("CONS") for r in cons_rows)
        # Article number (roman) is enclosed and populated on every row.
        assert all(r[12] == "I" for r in cons_rows)

    def test_first_row_known_values(self, cons_rows):
        assert cons_rows[0] == [
            "CONSSECTION 1.1974901.I",
            "CONS",
            "SECTION 1.",
            "1974",
            "90",
            "1",
            None,
            "id_b6f6901d-291e-11d9-b4a4-98a877856a41",
            None,
            None,
            None,
            None,
            "I",
            ("Sec. 1 added Nov. 5, 1974, by Proposition 7. "
             "Resolution Chapter 90, 1974."),
            "LAW_SECTION_TBL_314.lob",
            "Y",
            "LEG_ESI",
            "2025-12-17 01:00:03",
        ]

    def test_null_positions(self, cons_rows):
        # effective_date and the division/title/part/chapter block are NULL on
        # every CONS row; two rows have additional NULLs (chapter / op_section).
        for row in cons_rows:
            for idx in (6, 8, 9, 10, 11):
                assert row[idx] is None
        assert cons_rows[4][2] == "SEC. 7." and cons_rows[4][5] is None
        assert cons_rows[7][2] == "SEC. 14.1." and cons_rows[7][4] is None


# ---------------------------------------------------------------------------
# escaped_rows.dat — real rows containing backslash escapes (slow path)
# ---------------------------------------------------------------------------


class TestEscapedRows:
    def test_row_and_column_counts(self, escaped_rows):
        assert len(escaped_rows) == 10
        assert {len(r) for r in escaped_rows} == {LAW_SECTION_NCOLS}

    def test_known_ids(self, escaped_rows):
        assert [r[0] for r in escaped_rows] == [
            "RTC34011.20225619",
            "RTC34011.1.20225621",
            "RTC34012.20225623",
            "EDC66093.3.20251248",
            "PRC75121.202628100",
            "EDC47606.3.20251246",
            "MVC998.547.20262898",
            "HSC50423.20262879",
            "HSC50900.20262883",
            "HSC50913.20262887",
        ]

    def test_escapes_are_resolved(self, escaped_rows):
        # The raw file contains backslashes (escape prefixes); no parsed
        # field retains one, and record/field structure survives — escaped
        # terminators become field CONTENT (real newlines/tabs), they never
        # split rows or fields.
        for row in escaped_rows:
            for value in row:
                if value is not None:
                    assert "\\" not in value

    def test_escaped_n_decodes_to_newline(self, escaped_rows):
        # MySQL LOAD DATA control escapes: "\n" in the file is a linefeed in
        # the data (a naive \<c> -> <c> mapping yielded the letter "n" —
        # "Sec. 41.  nn   Repealed" — a real corruption fixed in 2026-08).
        history = escaped_rows[0][13]
        assert "Sec. 41.  \n\n   Repealed conditionally" in history
        assert history.startswith("Amended by Stats. 2022, Ch. 56, Sec. 19.")
        assert "nn" not in history


# ---------------------------------------------------------------------------
# Focused synthetic cases
# ---------------------------------------------------------------------------


class TestSynthetic:
    def test_escaped_backtick_inside_enclosed_field(self):
        assert parse_text("`a\\`b`\t`c`\n") == [["a`b", "c"]]

    def test_escaped_tab_in_bare_field(self):
        # The escaped tab becomes field content, not a terminator.
        assert parse_text("a\\\tb\tc\n") == [["a\tb", "c"]]

    def test_escaped_backslash(self):
        assert parse_text("a\\\\b\tc\n") == [["a\\b", "c"]]

    def test_backtick_mid_field_does_not_close(self):
        # Slow path: backtick followed by a non-terminator is literal content.
        assert parse_text("`a`b`\t\\N\n") == [["a`b", None]]
        # Fast path agrees (only leading/trailing backticks are stripped).
        assert parse_text("`a`b`\tc\n") == [["a`b", "c"]]

    def test_final_record_without_trailing_newline(self):
        assert parse_text("NULL\tx") == [[None, "x"]]         # fast path
        assert parse_text("`a\\`b`\t`c`") == [["a`b", "c"]]   # slow path, EOF
        # EOF also closes an enclosed field.
        assert parse_text("`a`") == [["a"]]

    def test_empty_enclosed_field(self):
        assert parse_text("``\t`x`\n") == [["", "x"]]          # fast path
        assert parse_text("``\t`x`\t\\N\n") == [["", "x", None]]  # slow path

    def test_null_semantics(self):
        # Bare NULL and \N are SQL NULL; enclosed `NULL` is the literal
        # string; other bare text (even lowercase null) passes through.
        assert parse_text("NULL\t`NULL`\t\\N\tnull\n") == [
            [None, "NULL", None, "null"]
        ]

    def test_latin1_fallback(self):
        # 0xE9 is invalid UTF-8 here; the parser falls back to latin-1.
        assert parse_bytes(b"`caf\xe9`\tNULL\n") == [["caf\xe9", None]]
        # Valid UTF-8 input decodes as UTF-8.
        assert parse_bytes("`café`\tx\n".encode()) == [
            ["café", "x"]
        ]

    def test_multiple_rows_and_blank_line_handling(self):
        assert parse_text("a\tb\nc\td\n") == [["a", "b"], ["c", "d"]]

    def test_fast_and_slow_paths_agree(self):
        # Identical escape-free rows must parse identically whether or not a
        # backslash elsewhere in the file forces the state-machine path.
        base = "`a`\tNULL\t``\t`x`y`\tbare\n`NULL`\t`z`\tNULL\t`q`\tr\n"
        expected = [
            ["a", None, "", "x`y", "bare"],
            ["NULL", "z", None, "q", "r"],
        ]
        assert parse_text(base) == expected                    # fast path
        variant = base + "esc\\aped\t\\N\n"                    # slow path
        assert parse_text(variant) == expected + [["escaped", None]]

    def test_fixture_fast_slow_equivalence(self, datfiles):
        # Appending an escaped row to a real escape-free fixture must not
        # change how the original rows parse.
        text = (datfiles / "law_section_slice.dat").read_text("utf-8")
        assert "\\" not in text
        fast = parse_text(text)
        slow = parse_text(text + "tail\\`ed\t\\N\n")
        assert slow[:-1] == fast
        assert slow[-1] == ["tail`ed", None]

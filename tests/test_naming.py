"""Unit tests for server/naming.py — code aliases, section citations,
measures, sessions."""

import pytest

from server.naming import (
    code_suggestions,
    norm_session,
    parse_measure,
    parse_section,
    resolve_code,
)


@pytest.mark.parametrize("text,code", [
    ("GOV", "GOV"),
    ("gov", "GOV"),
    ("Gov. Code", "GOV"),
    ("Govt Code", "GOV"),
    ("Government Code", "GOV"),
    ("Cal. Gov. Code", "GOV"),
    ("Welfare and Institutions Code", "WIC"),
    ("Welf. & Inst. Code", "WIC"),
    ("W&I", "WIC"),
    ("Code of Civil Procedure", "CCP"),
    ("Civil Procedure", "CCP"),
    ("CCP", "CCP"),
    ("Civ. Proc.", "CCP"),
    ("Civil Code", "CIV"),
    ("Civ. Code", "CIV"),
    ("Pen. Code", "PEN"),
    ("PC", "PEN"),
    ("Penal Code", "PEN"),
    ("B&P", "BPC"),
    ("Bus. & Prof. Code", "BPC"),
    ("Health & Safety Code", "HSC"),
    ("H&S Code", "HSC"),
    ("Cal. Const.", "CONS"),
    ("California Constitution", "CONS"),
    ("Constitution", "CONS"),
    ("CONS", "CONS"),
    ("Veh. Code", "VEH"),
    ("CVC", "VEH"),
    ("Rev. & Tax. Code", "RTC"),
    ("Pub. Res. Code", "PRC"),
    ("Public Contract Code", "PCC"),
    ("Sts. & Hy. Code", "SHC"),
    ("Ed. Code", "EDC"),
    ("Evid. Code", "EVID"),
    ("Fam. Code", "FAM"),
    ("Unemp. Ins. Code", "UIC"),
    ("Food & Agricultural Code", "FAC"),
    ("Fish and Game Code", "FGC"),
    ("Mil. & Vet. Code", "MVC"),
    ("Harb. & Nav. Code", "HNC"),
])
def test_resolve_code(text, code):
    assert resolve_code(text) == code


def test_resolve_code_unknown():
    assert resolve_code("Tax Code of 1986") is None
    assert resolve_code("") is None


@pytest.mark.parametrize("text,code", [
    ("C.C.P.", "CCP"),
    ("P.C.", "PEN"),
    ("V.C.", "VEH"),
    ("U.I.C.", "UIC"),
    ("CA Penal Code", "PEN"),
    ("Ca. Civ. Code", "CIV"),
    ("CA Gov Code", "GOV"),
])
def test_resolve_code_dotted_and_ca_prefixed(text, code):
    assert resolve_code(text) == code


def test_code_suggestions_ranked():
    sugg = code_suggestions("Goverment Code")  # typo
    assert any("GOV" in s for s in sugg)


# --- section citations ---------------------------------------------------

@pytest.mark.parametrize("text,key", [
    ("54957.5", "54957.5"),
    ("54957.5.", "54957.5"),
    ("§ 54957.5", "54957.5"),
    ("Section 1050", "1050"),
    ("Sec. 1050", "1050"),
    ("sec 1050", "1050"),
])
def test_parse_section_plain(text, key):
    assert parse_section("GOV", text) == ("plain", key)


@pytest.mark.parametrize("text,key", [
    ("Art. XIII B, Sec. 1", "Art. XIII B, Sec. 1"),
    ("Article XIII B, Section 1", "Art. XIII B, Sec. 1"),
    ("article 13B, section 1", "Art. XIII B, Sec. 1"),
    ("Art. XIIIA, § 3", "Art. XIII A, Sec. 3"),
    ("Article 1, Section 32", "Art. I, Sec. 32"),
    ("Art. I Sec. 1", "Art. I, Sec. 1"),
    ("Art. XIII C, Sec. 2", "Art. XIII C, Sec. 2"),
])
def test_parse_section_cons(text, key):
    assert parse_section("CONS", text) == ("cons", key)


@pytest.mark.parametrize("text,article", [
    ("Art. XXII", "XXII"),
    ("Article XIII B", "XIII B"),
    ("Article 4", "IV"),
    ("art XIIIA", "XIII A"),
])
def test_parse_section_cons_article_only(text, article):
    assert parse_section("CONS", text) == ("cons_article", article)


def test_parse_section_cons_unscoped():
    kind, key = parse_section("CONS", "1")
    assert kind == "cons_need_article" and key is None


@pytest.mark.parametrize("text,key", [
    ("Section 1 of Article XIII B", "Art. XIII B, Sec. 1"),
    ("Sec. 3 of Article I", "Art. I, Sec. 3"),
    ("§ 32 of Art. 1", "Art. I, Sec. 32"),
])
def test_parse_section_cons_reverse_order(text, key):
    # The Legislative Counsel's own citation order.
    assert parse_section("CONS", text) == ("cons", key)


@pytest.mark.parametrize("text,key", [
    ("54957.5(b)", "54957.5"),
    ("54957.5, subd. (b)", "54957.5"),
    ("1050(e)(2)", "1050"),
    ("Section 12940, subdivision (a)(1)", "12940"),
])
def test_parse_section_strips_subdivisions(text, key):
    assert parse_section("GOV", text) == ("plain", key)


# --- measures ------------------------------------------------------------

def test_parse_measure_forms():
    assert parse_measure("AB 831") == {"type": "AB", "num": "831"}
    assert parse_measure("ab831") == {"type": "AB", "num": "831"}
    assert parse_measure("SB-1421") == {"type": "SB", "num": "1421"}
    assert parse_measure("ACA 13") == {"type": "ACA", "num": "13"}
    assert parse_measure("garbage input") is None


def test_parse_measure_dotted_bluebook_forms():
    # The citation format court filings actually use.
    assert parse_measure("A.B. 831") == {"type": "AB", "num": "831"}
    assert parse_measure("S.B. 1421") == {"type": "SB", "num": "1421"}
    assert parse_measure("SB 1421.") == {"type": "SB", "num": "1421"}


def test_parse_measure_bill_id():
    pm = parse_measure("202520260AB13")
    assert pm == {"bill_id": "202520260AB13", "session_year": "20252026",
                  "type": "AB", "num": "13"}


# --- sessions ------------------------------------------------------------

@pytest.mark.parametrize("text,sy", [
    ("2023-2024", "20232024"),
    ("2023-24", "20232024"),
    ("2023", "20232024"),
    ("2024", "20232024"),  # even year -> session started the year before
    ("20232024", "20232024"),
    ("1989", "19891990"),
    ("1990", "19891990"),
    ("1999-00", "19992000"),
    ("bogus", None),
    ("23-24", None),        # two-digit shorthand is ambiguous, rejected
    ("20242025", None),     # even start year is not a real session
    ("20232025", None),     # non-consecutive years
])
def test_norm_session(text, sy):
    assert norm_session(text) == sy

"""Unit tests for ingest/titles.py — the Legislative Counsel title parser.

Two layers:

1. Golden-file test: tests/fixtures/titles_golden.json holds 49 real
   2025-26 session titles with expected status and refs. The file is
   regenerated (scripts kept in the session scratchpad) whenever parser
   behavior intentionally changes; CONS and multi-section entries were
   hand-checked after the 2026-08 fix batch (Oxford-comma final-section
   drop, dead CONS-article branch, session-law guard).

2. Hand-written semantic cases, verified by reading the title text —
   including the known typo tolerances (missing "Section", missing "to",
   "Welfare and Institution Code", arabic and glued article numbers).
"""

import dataclasses
import json
from pathlib import Path

import pytest

from ingest.titles import Ref, parse_title

GOLDEN = json.loads(
    (Path(__file__).parent / "fixtures" / "titles_golden.json").read_text())


def refs_as_dicts(result):
    return [dataclasses.asdict(r) for r in result.refs]


# ---------------------------------------------------------------------------
# 1. Golden file: 49 real titles, exact reproduction.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "entry", GOLDEN,
    ids=[f"{i:02d}-{e['status']}" for i, e in enumerate(GOLDEN)])
def test_golden(entry):
    result = parse_title(entry["title"])
    assert result.status == entry["status"]
    assert refs_as_dicts(result) == entry["refs"]


def test_golden_fixture_shape():
    assert len(GOLDEN) == 49
    assert {e["status"] for e in GOLDEN} == {
        "ok", "budget_act", "uncodified", "no_sections"}


# ---------------------------------------------------------------------------
# 2. Hand-written semantic cases.
# ---------------------------------------------------------------------------

def test_simple_amend():
    r = parse_title(
        "An act to amend Section 12100.65 of the Government Code, "
        "relating to economic development.")
    assert r.status == "ok"
    assert r.refs == [Ref("amend", "GOV", "12100.65")]


def test_compound_multi_verb_deferred_code():
    # The module docstring's own example: three verb groups, all targeting
    # the deferred "the Civil Code" at the end. The Oxford-comma list must
    # be complete — a split-based _split_seclist used to drop 942.
    r = parse_title(
        "An act to amend Sections 910, 930, and 942 of, to add Sections "
        "942.2 and 945.1 to, and to repeal and add Section 926 of, the "
        "Civil Code, relating to construction defects.")
    assert r.status == "ok"
    assert r.refs == [
        Ref("amend", "CIV", "910"),
        Ref("amend", "CIV", "930"),
        Ref("amend", "CIV", "942"),
        Ref("add", "CIV", "942.2"),
        Ref("add", "CIV", "945.1"),
        Ref("repeal and add", "CIV", "926"),
    ]


def test_two_section_and_list_is_complete():
    r = parse_title(
        "An act to add Sections 942.2 and 945.1 to the Civil Code, "
        "relating to construction defects.")
    assert r.status == "ok"
    assert r.refs == [
        Ref("add", "CIV", "942.2"),
        Ref("add", "CIV", "945.1"),
    ]


def test_range_inclusive():
    r = parse_title(
        "An act to repeal Sections 200 to 262, inclusive, of the "
        "Education Code, relating to education.")
    assert r.status == "ok"
    assert r.refs == [
        Ref("repeal", "EDC", "200", is_range=True, range_end="262")]


def test_mixed_list_and_range():
    # A range in the middle of a list, and sections after it — every
    # element must survive.
    r = parse_title(
        "An act to repeal Sections 100, 200 to 262, inclusive, and 300 "
        "of the Education Code, relating to education.")
    assert r.status == "ok"
    assert r.refs == [
        Ref("repeal", "EDC", "100"),
        Ref("repeal", "EDC", "200", is_range=True, range_end="262"),
        Ref("repeal", "EDC", "300"),
    ]


def test_struct_commencing_with():
    r = parse_title(
        "An act to add Chapter 4.5 (commencing with Section 1234) to "
        "Part 1 of Division 2 of the Labor Code, relating to employment.")
    assert r.status == "ok"
    assert r.refs == [Ref("add", "LAB", "1234", struct="Chapter 4.5")]


def test_budget_act():
    r = parse_title(
        "An act to amend the Budget Act of 2025 by amending Item "
        "0509-001-0001 of Section 2.00 of that act, relating to the state "
        "budget, and making an appropriation therefor, to take effect "
        "immediately, budget bill.")
    assert r.status == "budget_act"
    assert r.refs == []


def test_uncodified_statute():
    r = parse_title(
        "An act to amend Section 52 of Chapter 428 of the Statutes of "
        "1913, relating to the West Side Irrigation District.")
    assert r.status == "uncodified"
    assert r.refs == []


def test_session_law_section_not_attributed_to_code():
    # A session-law section citation next to a real code ref: the
    # "Statutes of" guard must skip it rather than attribute it to the
    # nearest code name.
    r = parse_title(
        "An act to amend Section 6 of the Improvement Act, Statutes of "
        "1913, and to amend Section 100 of the Water Code, relating to "
        "assessments.")
    assert Ref("amend", "WAT", "100") in r.refs
    assert not any(ref.section == "6" for ref in r.refs)


def test_resolution_relative_to():
    r = parse_title(
        "Relative to Section 230 of the federal Communications Decency "
        "Act of 1996.")
    assert r.status == "no_sections"
    assert r.refs == []


def test_constitution_ref_article_scoped():
    # CONS refs carry the canonical article-scoped key so they join
    # law_section.section_num_norm directly.
    r = parse_title(
        "A resolution to propose to the people of the State of California "
        "an amendment to the Constitution of the State, by amending "
        "Section 1 of Article XIII B of the California Constitution, "
        "relating to government spending.")
    assert r.status == "ok"
    assert r.refs == [Ref("amending", "CONS", "Art. XIII B, Sec. 1")]


def test_constitution_deferred_article():
    # Real ACA form: the article is deferred past two clauses — resolved
    # positionally (nearest following Article mention), like code names.
    r = parse_title(
        "A resolution to propose to the people of the State of California "
        "an amendment to the Constitution of the State, by amending "
        "Sections 9 and 10 of, and adding Section 7.5 to, Article II "
        "thereof, relating to elections.")
    assert r.status == "ok"
    assert r.refs == [
        Ref("amending", "CONS", "Art. II, Sec. 9"),
        Ref("amending", "CONS", "Art. II, Sec. 10"),
        Ref("adding", "CONS", "Art. II, Sec. 7.5"),
    ]


def test_constitution_glued_article():
    # "Article XIIIA" (no space) appears in real titles -> "Art. XIII A".
    r = parse_title(
        "A resolution to propose to the people of the State of California "
        "an amendment to the Constitution of the State, by amending "
        "Section 3 of Article XIIIA thereof, relating to taxation.")
    assert r.status == "ok"
    assert r.refs == [Ref("amending", "CONS", "Art. XIII A, Sec. 3")]


def test_arabic_article_typo():
    # "Article 1" (arabic, real Leg Counsel typo, ACA 7 2025 style).
    r = parse_title(
        "A resolution to propose to the people of the State of California "
        "an amendment to the Constitution of the State, by adding "
        "Section 32 to Article 1 thereof, relating to civil rights.")
    assert r.status == "ok"
    assert r.refs == [Ref("adding", "CONS", "Art. I, Sec. 32")]


def test_article_thereof_resolves_to_cons():
    # ACA/SCA style: "Article IV thereof" refers back to the Constitution.
    r = parse_title(
        "A resolution to propose to the people of the State of California "
        "an amendment to the Constitution of the State, by adding "
        "Section 23 to Article IV thereof, relating to the Legislature.")
    assert r.status == "ok"
    assert r.refs == [Ref("adding", "CONS", "Art. IV, Sec. 23")]


def test_urgency_thereof_not_a_cons_ref():
    # "declaring the urgency thereof" must NOT be rewritten into a
    # California Constitution reference.
    r = parse_title(
        "An act to amend Section 8588 of the Government Code, relating to "
        "emergency services, and declaring the urgency thereof, to take "
        "effect immediately.")
    assert r.status == "ok"
    assert r.refs == [Ref("amend", "GOV", "8588")]
    assert all(ref.code != "CONS" for ref in r.refs)


def test_urgency_only_act_no_sections():
    r = parse_title(
        "An act relating to fire prevention, and declaring the urgency "
        "thereof, to take effect immediately.")
    assert r.status == "no_sections"
    assert r.refs == []


def test_missing_section_word_typo():
    r = parse_title(
        "An act to amend 7928.205 of the Government Code, relating to "
        "public records.")
    assert r.status == "ok"
    assert r.refs == [Ref("amend", "GOV", "7928.205")]


def test_missing_to_typo():
    r = parse_title(
        "An act amend Section 1234 of the Government Code, relating to "
        "state government.")
    assert r.status == "ok"
    assert r.refs == [Ref("amend", "GOV", "1234")]


def test_welfare_and_institution_code_typo():
    r = parse_title(
        "An act to amend Section 14005.27 of the Welfare and Institution "
        "Code, relating to Medi-Cal.")
    assert r.status == "ok"
    assert r.refs == [Ref("amend", "WIC", "14005.27")]


def test_appropriations_boilerplate_ignored():
    r = parse_title(
        "An act making appropriations for the support of the government "
        "of the State of California and for several public purposes in "
        "accordance with the provisions of Section 12 of Article IV of "
        "the Constitution of the State of California, relating to the "
        "state budget, to take effect immediately, budget bill.")
    assert r.status == "no_sections"
    assert r.refs == []

"""Unit tests for ingest/normalize.py — canonical section keys.

Covers to_roman, norm_section, norm_article, cons_key (all four real
section-prefix spellings seen in LAW_SECTION_TBL / titles), and
law_section_key's CONS vs normal-code vs None handling.
"""

import pytest

from ingest.normalize import (
    cons_key,
    law_section_key,
    norm_article,
    norm_section,
    to_roman,
)


class TestToRoman:
    @pytest.mark.parametrize(
        ("n", "expected"),
        [
            (1, "I"),
            (4, "IV"),
            (9, "IX"),
            (14, "XIV"),
            (35, "XXXV"),
            (40, "XL"),
        ],
    )
    def test_values(self, n, expected):
        assert to_roman(n) == expected


class TestNormSection:
    def test_strips_single_trailing_period(self):
        # LAW_SECTION_TBL stores "44955." — lookups use the stripped form.
        assert norm_section("44955.") == "44955"

    def test_keeps_internal_period(self):
        assert norm_section("1.5.") == "1.5"

    def test_no_trailing_period_unchanged(self):
        assert norm_section("12100.65") == "12100.65"

    def test_strips_surrounding_whitespace(self):
        assert norm_section("  44955. ") == "44955"

    def test_strips_multiple_trailing_periods(self):
        assert norm_section("100..") == "100"


class TestNormArticle:
    def test_uppercases(self):
        assert norm_article("xiii b") == "XIII B"

    def test_already_canonical(self):
        assert norm_article("XIII B") == "XIII B"

    def test_arabic_to_roman(self):
        # "Article 1" is a real Leg Counsel typo — converted to roman.
        assert norm_article("1") == "I"

    def test_arabic_to_roman_multi_digit(self):
        assert norm_article("14") == "XIV"

    def test_collapses_internal_whitespace(self):
        assert norm_article("xiii   b") == "XIII B"


class TestConsKey:
    # All four spellings observed in real data: the law table stores
    # "SEC. 1." / "Sec. 1."; titles cite bare numbers; "SECTION 1." and
    # "Section 1." also occur. All must collapse to the same key.
    @pytest.mark.parametrize(
        "section_num",
        ["SEC. 1.", "Sec. 1.", "SECTION 1.", "Section 1."],
    )
    def test_all_real_spellings(self, section_num):
        assert cons_key("I", section_num) == "Art. I, Sec. 1"

    def test_bare_number(self):
        # Bill titles cite "Section 1 of Article XIII B" — bare number side.
        assert cons_key("XIII B", "1") == "Art. XIII B, Sec. 1"

    def test_lettered_article_lowercase(self):
        assert cons_key("xiii b", "Sec. 1.") == "Art. XIII B, Sec. 1"

    def test_arabic_article_normalized(self):
        assert cons_key("4", "SEC. 3.") == "Art. IV, Sec. 3"

    def test_law_table_and_title_sides_agree(self):
        # The whole point: both sides land on the same join key.
        law_side = cons_key("XIII B", "SEC. 1.")
        title_side = cons_key("XIII B", "1")
        assert law_side == title_side == "Art. XIII B, Sec. 1"


class TestLawSectionKey:
    def test_cons_with_article(self):
        assert law_section_key("CONS", "SEC. 1.", "XIII B") == \
            "Art. XIII B, Sec. 1"

    def test_cons_with_arabic_article(self):
        assert law_section_key("CONS", "SEC. 3.", "1") == "Art. I, Sec. 3"

    def test_normal_code_strips_trailing_period(self):
        assert law_section_key("GOV", "44955.", None) == "44955"

    def test_normal_code_ignores_article(self):
        # Non-CONS rows carry article values too — they must not be folded
        # into the key.
        assert law_section_key("EDC", "44955.", "2") == "44955"

    def test_none_code(self):
        assert law_section_key(None, "100.", None) == "100"

    def test_none_section_num_is_none(self):
        assert law_section_key("GOV", None, None) is None

    def test_none_section_num_cons_is_none(self):
        assert law_section_key("CONS", None, "I") is None

    def test_cons_without_article_falls_back_to_norm_section(self):
        # Current behavior: a CONS row missing its article gets only the
        # trailing-period strip, leaving the "SEC." prefix in the key.
        # CONS rows in the real law table always carry an article, so this
        # is an unreachable-in-practice fallback, documented as-is.
        assert law_section_key("CONS", "SEC. 1.", None) == "SEC. 1"

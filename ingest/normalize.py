"""Canonical section keys, shared by the law loader and the title parser.

Two normalization problems (SPEC §6):

- Code sections carry trailing periods in LAW_SECTION_TBL ("44955.") —
  strip them; lookups and joins use the stripped form.
- Constitution sections are article-scoped. LAW_SECTION_TBL stores
  section_num="SEC. 1." with the article in its own column ("XIII B");
  bill titles cite "Section 1 of Article XIII B". Both sides normalize to
  the same key: ``Art. XIII B, Sec. 1``. Titles occasionally use arabic
  article numbers ("Article 1", a real Leg Counsel typo) — converted to
  roman so the join still lands.
"""

from __future__ import annotations

import re

_ROMAN_VALUES = (
    (50, "L"), (40, "XL"), (10, "X"), (9, "IX"),
    (5, "V"), (4, "IV"), (1, "I"),
)

_SEC_PREFIX = re.compile(r"^sec(?:tion)?\.?\s*", re.IGNORECASE)


def to_roman(n: int) -> str:
    out = []
    for value, numeral in _ROMAN_VALUES:
        while n >= value:
            out.append(numeral)
            n -= value
    return "".join(out)


def norm_section(num: str) -> str:
    """'44955.' -> '44955'; '1.5.' -> '1.5'."""
    return num.strip().rstrip(".")


def norm_article(article: str) -> str:
    """'xiii b' -> 'XIII B'; '1' -> 'I'; 'XIIIA' -> 'XIII A' (glued suffix
    letters appear in real titles); collapse internal whitespace."""
    art = " ".join(article.split()).upper()
    art = re.sub(r"^([IVXLC]+)([A-D])$", r"\1 \2", art)
    if art.isdigit():
        art = to_roman(int(art))
    else:
        m = re.fullmatch(r"(\d+)\s*([A-D])", art)
        if m:
            art = f"{to_roman(int(m.group(1)))} {m.group(2)}"
    return art


def cons_key(article: str, section_num: str) -> str:
    """Canonical key for a Constitution section, e.g. 'Art. XIII B, Sec. 1'.

    section_num accepts both the law-table form ('SEC. 1.') and a bare
    number ('1').
    """
    num = norm_section(_SEC_PREFIX.sub("", section_num.strip()))
    return f"Art. {norm_article(article)}, Sec. {num}"


def law_section_key(law_code: str | None, section_num: str | None,
                    article: str | None) -> str | None:
    """section_num_norm for a LAW_SECTION_TBL row."""
    if section_num is None:
        return None
    if law_code == "CONS" and article:
        return cons_key(article, section_num)
    return norm_section(section_num)

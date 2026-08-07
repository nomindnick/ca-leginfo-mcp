"""User-input normalization: code names and section citations.

Attorneys cite codes in many forms — "Gov. Code", "Govt Code", "GOV",
"Government Code", "Welf. & Inst. Code", "CCP", "Cal. Const." — all of
which must resolve to the pubinfo code letters (GOV, WIC, CCP, CONS…).
The alias table below covers the California Style Manual abbreviations
plus the common initialisms; matching is case/punctuation-insensitive
("&" ≡ "and", periods and a trailing "Code" are ignored).

Constitution sections are article-scoped (SPEC §6): input like
"Art. XIII B, Sec. 1" / "Article 13A, Section 3" normalizes to the
canonical key ingest produces ("Art. XIII B, Sec. 1"). Article-only
input is recognized so tools can respond with the article's section
list instead of a dead end.
"""

from __future__ import annotations

import difflib
import re

from ingest.normalize import cons_key, norm_article, norm_section

# code -> (canonical display name, alias phrases). The code letters
# themselves and the display name are implicit aliases; phrases are
# matched after _canon() (see below), so "Welf. & Inst." ≡ "welf and inst".
CODE_ALIASES: dict[str, tuple[str, tuple[str, ...]]] = {
    "BPC": ("Business and Professions Code", ("bus and prof", "b and p")),
    "CCP": ("Code of Civil Procedure",
            ("civ proc", "civil procedure", "code civ proc")),
    "CIV": ("Civil Code", ("civ",)),
    "COM": ("Commercial Code", ("com", "comm")),
    "CONS": ("California Constitution",
             ("constitution", "const", "cal const", "ca const",
              "california constitution")),
    "CORP": ("Corporations Code", ("corp", "corps")),
    "EDC": ("Education Code", ("ed", "educ")),
    "ELEC": ("Elections Code", ("elec",)),
    "EVID": ("Evidence Code", ("evid", "ev")),
    "FAC": ("Food and Agricultural Code",
            ("food and agr", "food and ag", "food and agriculture")),
    "FAM": ("Family Code", ("fam",)),
    "FGC": ("Fish and Game Code", ("fish and g", "fish and game", "f and g")),
    "FIN": ("Financial Code", ("fin",)),
    "GOV": ("Government Code", ("gov", "govt", "gov t")),
    "HNC": ("Harbors and Navigation Code", ("harb and nav", "h and n")),
    "HSC": ("Health and Safety Code",
            ("health and saf", "h and s", "health and safety")),
    "INS": ("Insurance Code", ("ins",)),
    "LAB": ("Labor Code", ("lab",)),
    "MVC": ("Military and Veterans Code",
            ("mil and vet", "military and veterans")),
    "PCC": ("Public Contract Code",
            ("pub contract", "pub cont", "public contracts")),
    "PEN": ("Penal Code", ("pen", "pc")),
    "PRC": ("Public Resources Code", ("pub res", "pub resources")),
    "PROB": ("Probate Code", ("prob",)),
    "PUC": ("Public Utilities Code", ("pub util", "pub u", "pu")),
    "RTC": ("Revenue and Taxation Code",
            ("rev and tax", "r and t", "revenue and tax")),
    "SHC": ("Streets and Highways Code",
            ("sts and hy", "s and h", "streets and highways")),
    "UIC": ("Unemployment Insurance Code", ("unemp ins", "ui")),
    "VEH": ("Vehicle Code", ("veh", "vc", "cvc")),
    "WAT": ("Water Code", ("wat", "water")),
    "WIC": ("Welfare and Institutions Code",
            ("welf and inst", "w and i", "welfare and institution")),
}


def _canon(text: str) -> str:
    """Lowercase; '&'->'and'; strip periods/commas/apostrophes; collapse
    whitespace; drop a leading 'cal'/'ca'/'california' and a trailing
    'code'."""
    t = text.lower().replace("&", " and ")
    t = re.sub(r"[.,'’]", " ", t)
    t = " ".join(t.split())
    t = re.sub(r"^(?:ca|cal|calif|california)\s+(?=\S)", "", t)
    # "code of civil procedure" keeps its leading "code of".
    if t != "code of civil procedure":
        t = re.sub(r"\s+code$", "", t)
    return t


def _build_lookup() -> dict[str, str]:
    lookup: dict[str, str] = {}
    for code, (name, phrases) in CODE_ALIASES.items():
        for alias in (code, name, *phrases):
            lookup[_canon(alias)] = code
    # "code of civil procedure" also arrives without the leading "code of".
    lookup[_canon("civil procedure")] = "CCP"
    return lookup


_LOOKUP = _build_lookup()


def resolve_code(text: str) -> str | None:
    """'Gov. Code' -> 'GOV'; None when unrecognized.

    Dotted initialisms ('C.C.P.', 'P.C.', 'V.C.') canonicalize with
    internal spaces under _canon, so a second lookup with the periods
    removed outright covers them.
    """
    t = str(text)
    return _LOOKUP.get(_canon(t)) or _LOOKUP.get(_canon(t.replace(".", "")))


def code_suggestions(text: str, n: int = 5) -> list[str]:
    """Closest known code names for an unrecognized input."""
    close = difflib.get_close_matches(_canon(str(text)), _LOOKUP.keys(),
                                      n=n, cutoff=0.4)
    seen: dict[str, None] = {}
    for c in close:
        code = _LOOKUP[c]
        seen.setdefault(f"{code} — {CODE_ALIASES[code][0]}", None)
    return list(seen)


# --- section input -------------------------------------------------------

_PLAIN_PREFIX = re.compile(r"^\s*(?:§+|sec(?:tion)?\.?)\s*", re.IGNORECASE)
# Trailing subdivision citations — "(b)", "(b)(1)", ", subd. (b)" — are
# below the section level the law tables key on; strip them for lookup.
_SUBDIVISION = re.compile(
    r"[,\s]*(?:subd(?:ivision)?s?\.?\s*)?(?:\([a-zA-Z0-9]{1,3}\)\s*)+$")
_ARTICLE_TOKEN = r"([0-9]+|[IVXLC]+)((?:\s*[A-D])?)"
_CONS_BOTH = re.compile(
    rf"art(?:icle)?\.?\s*{_ARTICLE_TOKEN}\s*[,;]?\s*"
    r"(?:§+|sec(?:tion)?\.?)\s*([0-9][\w.]*)",
    re.IGNORECASE)
# The Legislative Counsel's own order: "Section 1 of Article XIII B".
_CONS_SEC_OF_ART = re.compile(
    rf"(?:§+|sec(?:tion)?\.?)\s*([0-9][\w.]*)\s+of\s+"
    rf"art(?:icle)?\.?\s*{_ARTICLE_TOKEN}",
    re.IGNORECASE)
_CONS_ARTICLE_ONLY = re.compile(
    rf"^\s*art(?:icle)?\.?\s*{_ARTICLE_TOKEN}\s*\.?\s*$", re.IGNORECASE)


def parse_section(code: str, text: str) -> tuple[str, str | None]:
    """Normalize a section citation for lookup.

    Returns (kind, key):
      ("plain", "54957.5")            — non-CONS section
      ("cons", "Art. XIII B, Sec. 1") — Constitution article+section
      ("cons_article", "XIII B")      — Constitution, article only
      ("cons_need_article", None)     — CONS input we couldn't scope
    """
    text = _SUBDIVISION.sub("", str(text).strip())
    if code != "CONS":
        return "plain", norm_section(_PLAIN_PREFIX.sub("", text))
    m = _CONS_SEC_OF_ART.search(text)
    if m:
        article = norm_article(f"{m.group(2)}{m.group(3)}")
        return "cons", cons_key(article, m.group(1))
    m = _CONS_BOTH.search(text)
    if m:
        article = norm_article(f"{m.group(1)}{m.group(2)}")
        return "cons", cons_key(article, m.group(3))
    m = _CONS_ARTICLE_ONLY.match(text)
    if m:
        return "cons_article", norm_article(f"{m.group(1)}{m.group(2)}")
    return "cons_need_article", None


# --- measures and sessions ----------------------------------------------

# Measure numbers are bounded ({1,6}): int() on an unbounded digit run
# raises ValueError past 4300 digits (CPython 3.11+), and these regexes
# see raw user input via every tool's measure/ref arguments.
_BILL_ID = re.compile(r"^\s*(\d{8})(\d)([A-Z]{1,4})(\d{1,6})\s*$")
_MEASURE = re.compile(r"^\s*([A-Za-z]{1,4})[\s.\-]*(\d{1,6})\s*$")


def parse_measure(text: str) -> dict | None:
    """'AB 831' / 'A.B. 831' / 'ab-831' -> {type, num}; a full bill_id
    ('202520260AB13') -> {bill_id, session_year, type, num}."""
    text = str(text).strip()
    m = _BILL_ID.match(text.upper().replace(" ", ""))
    if m:
        return {"bill_id": text.upper().replace(" ", ""),
                "session_year": m.group(1), "type": m.group(3),
                "num": str(int(m.group(4)))}
    # Dotted Bluebook forms ("A.B. 831", "S.B. 1421.") match once the
    # periods are gone.
    m = _MEASURE.match(text.replace(".", ""))
    if m:
        return {"type": m.group(1).upper(), "num": str(int(m.group(2)))}
    return None


def norm_session(text: str | int) -> str | None:
    """'2023', '2024', '2023-24', '2023-2024', '20232024' -> '20232024'.

    Sessions start in odd years; an even year belongs to the session that
    began the year before.
    """
    digits = re.sub(r"\D", "", str(text))
    if len(digits) == 8:
        start, end = int(digits[:4]), int(digits[4:])
        if end != start + 1:
            return None
    elif len(digits) == 6:  # '2023-24' (and '1999-00' across the century)
        start = int(digits[:4])
        end = start - start % 100 + int(digits[4:])
        if end < start:
            end += 100
    elif len(digits) == 4:
        y = int(digits)
        start = y if y % 2 == 1 else y - 1
        end = start + 1
    else:
        return None
    # Sessions are two consecutive years starting odd; anything else is a
    # garbled input, not a real session ('23-24' would otherwise become
    # the year 2324).
    if start % 2 == 0 or end != start + 1 or not 1849 <= start <= 2199:
        return None
    return f"{start}{end}"

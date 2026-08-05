"""Parse Legislative Counsel bill titles into (action, code, section) refs.

Titles are formulaic: "An act to amend Sections 910, 930, and 942 of, to add
Sections 942.2 and 945.1 to, and to repeal and add Section 926 of, the Civil
Code, relating to construction defects."

Strategy: find every Section/structural-unit mention in the act portion,
resolve each mention's verb (nearest preceding verb group) and target code
(nearest following code name — Legislative Counsel style always names the
code after the section list, deferring it in compound clauses).

Validated against the full 2025–26 session: 100% classification, zero
failures (SPIKE_FINDINGS.md). The typo tolerances below are all observed in
real titles, not speculative. Archive parse failures must be logged and
counted, never crash a build (SPEC §6).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ingest.normalize import cons_key

# Full code names as they appear in titles, mapped to capublic codes.
CODE_NAMES = {
    "Business and Professions Code": "BPC",
    "Code of Civil Procedure": "CCP",
    "Civil Code": "CIV",
    "Commercial Code": "COM",
    "Uniform Commercial Code": "COM",
    "Corporations Code": "CORP",
    "Education Code": "EDC",
    "Elections Code": "ELEC",
    "Evidence Code": "EVID",
    "Family Code": "FAM",
    "Financial Code": "FIN",
    "Fish and Game Code": "FGC",
    "Food and Agricultural Code": "FAC",
    "Government Code": "GOV",
    "Harbors and Navigation Code": "HNC",
    "Health and Safety Code": "HSC",
    "Insurance Code": "INS",
    "Labor Code": "LAB",
    "Military and Veterans Code": "MVC",
    "Penal Code": "PEN",
    "Probate Code": "PROB",
    "Public Contract Code": "PCC",
    "Public Resources Code": "PRC",
    "Public Utilities Code": "PUC",
    "Revenue and Taxation Code": "RTC",
    "Streets and Highways Code": "SHC",
    "Unemployment Insurance Code": "UIC",
    "Vehicle Code": "VEH",
    "Water Code": "WAT",
    "Welfare and Institutions Code": "WIC",
    "Welfare and Institution Code": "WIC",  # Leg Counsel typo, AB 753 (2025)
    "California Constitution": "CONS",
    "Constitution of the State": "CONS",
}

_CODE_RE = re.compile("|".join(
    re.escape(n) for n in sorted(CODE_NAMES, key=len, reverse=True)))

_VERB_WORDS = r"(?:amend|add|repeal|renumber|amending|adding|repealing|renumbering)"
# A verb group like "amend", "add and repeal", "amend, repeal, and add" —
# or the gerund style used by budget bills and constitutional amendment
# resolutions ("by amending Section..., and by adding Section...").
# "and" is a valid anchor for the gerund style's unrepeated preposition
# ("by amending Sections 9 and 10 of, and adding Section 7.5 to, ...") —
# compound groups like "repeal and add" are unaffected because left-to-right
# scanning consumes their "and <verb>" as a continuation of the first match.
_VERB_GROUP = re.compile(
    rf"\b(?:to|by|and)\s+((?:{_VERB_WORDS})(?:(?:,\s+|\s+and\s+|,\s+and\s+)"
    rf"(?:{_VERB_WORDS}))*)"
    rf"(?:\s+(?:the\s+heading(?:s)?\s+of|various\s+provisions\s+of))?\b",
    re.IGNORECASE)

_SECNUM = r"[0-9][0-9a-zA-Z.\-]*"
# One list item: a section number, optionally a range ("200 to 262,
# inclusive"). Ranges can appear anywhere in a list, not just at its end.
_SECITEM = rf"{_SECNUM}(?:\s+to\s+{_SECNUM}\s*,\s*inclusive)?"
# "Section 123" / "Sections 1, 2, and 3" / "Sections 200 to 262, inclusive"
# (lowercase "section" appears in real titles — Leg Counsel typos)
_SECTION_MENTION = re.compile(
    rf"\b[Ss]ections?\s+({_SECITEM}(?:\s*(?:,|,?\s+and)\s+{_SECITEM})*)")
_SECITEM_RE = re.compile(rf"({_SECNUM})(?:\s+to\s+({_SECNUM})\s*,\s*inclusive)?")
# "Chapter 4.5 (commencing with Section 1234)" — the word Section is
# sometimes missing or lowercased in real titles.
_STRUCT_MENTION = re.compile(
    rf"\b(Chapter|Article|Part|Division|Title)s?\s+([0-9IVXLC][0-9a-zA-Z.]*)"
    rf"\s*\(commencing\s+with\s+(?:[Ss]ection\s+)?({_SECNUM})\)")
# Any "Article XIII A" mention. Constitution refs name their article after
# the section list, deferring it in compound clauses exactly like code names
# ("Sections 9 and 10 of, and adding Section 7.5 to, Article II thereof") —
# so articles resolve positionally, like code_for. Usually roman, but arabic
# appears in real titles ("Article 1", ACA 7 2025).
_ARTICLE_MENTION = re.compile(r"\bArticle\s+([IVXLC0-9]+[\s]?[A-D]?)\b")
# Session-law refs like "Section 2 of Chapter 5 of the Statutes of 2011"
# are not code sections.
_STATUTES_REF = re.compile(
    r"\bof\s+(?:Chapter\s+\d+\s+of\s+)?the\s+.{0,30}Statutes(?:\s+of\s+\d{4})?")

_NO_SECTION_STARTS = (
    "relative to", "an act relating to", "an act making appropriations",
    "an act to make appropriations", "an act calling", "an act to provide",
    "an act to authorize", "an act granting", "an act concerning",
)


@dataclass
class Ref:
    action: str
    code: str | None
    section: str
    is_range: bool = False
    range_end: str | None = None
    struct: str | None = None  # e.g. "Chapter 4.5" for structural units


@dataclass
class ParseResult:
    status: str  # ok | no_sections | budget_act | uncodified | partial | fail
    refs: list[Ref] = field(default_factory=list)
    note: str = ""


def _split_seclist(text: str) -> list[tuple[str, bool, str | None]]:
    """'910, 930, and 942' / '200 to 262, inclusive' -> section tokens.

    Iterates items with finditer rather than splitting on connectors: a
    split-based approach silently dropped the final element of Oxford-comma
    lists (', and 942' -> token 'and 942' -> discarded) and any range not at
    the end of its list.
    """
    out = []
    for m in _SECITEM_RE.finditer(text):
        end = m.group(2).rstrip(".,") if m.group(2) else None
        out.append((m.group(1).rstrip(".,"), end is not None, end))
    return out


def parse_title(title: str) -> ParseResult:
    t = " ".join(title.split())
    # Boilerplate constitutional citation carried by every appropriations
    # bill — not an affected section.
    t = re.sub(r"in accordance with the provisions of Section 12 of "
               r"Article IV of the Constitution of the State of California",
               "", t)
    # Constitutional amendment resolutions say "Article IV thereof",
    # referring back to the Constitution. (Only after an Article ref —
    # "declaring the urgency thereof" must be left alone.)
    t = re.sub(r"(Article\s+[IVXLC0-9]+\s?[A-D]?)\s+of the California "
               r"Constitution|(Article\s+[IVXLC0-9]+\s?[A-D]?)\s+thereof",
               lambda m: (m.group(1) or m.group(2)) +
               " of the California Constitution", t)
    # Missing "to" after "An act" — another live typo pattern.
    t = re.sub(r"^An act (amend|add|repeal)\b", r"An act to \1", t)
    low = t.lower()

    # House/Senate resolutions never amend codes ("Relative to Section 230
    # of the federal Communications Decency Act" is commentary, not law).
    if low.startswith("relative to"):
        return ParseResult("no_sections", note="resolution")

    # Budget Act amendments target a session law, not a code — a distinct
    # category (their "Section 39.10" refs are sections of the Budget Act).
    if re.search(r"\bBudget Act of \d{4}\b", t):
        return ParseResult("budget_act", note="amends Budget Act")

    # Amendments to uncodified session law (district enabling acts, old
    # statutes chapters): there is no code section to link.
    act_portion = t.split(", relating to")[0]
    if re.search(r"Statutes of \d{4}", act_portion) and \
            not _CODE_RE.search(act_portion):
        return ParseResult("uncodified", note="amends uncodified statute")

    for prefix in _NO_SECTION_STARTS:
        if low.startswith(prefix) and not _SECTION_MENTION.search(t):
            return ParseResult("no_sections", note=prefix)

    # Act portion: cut the ", relating to ..." tail.
    act = t
    m = re.search(r",\s+relating\s+to\s+", t)
    if m:
        act = t[: m.start()]

    verb_positions = [(m.start(), m.group(1).lower())
                      for m in _VERB_GROUP.finditer(act)]
    code_positions = [(m.start(), CODE_NAMES[m.group(0)])
                      for m in _CODE_RE.finditer(act)]

    refs: list[Ref] = []
    unresolved = 0
    covered: list[tuple[int, int]] = []

    def verb_for(pos: int) -> str | None:
        prior = [v for p, v in verb_positions if p < pos]
        return prior[-1] if prior else None

    def code_for(pos: int) -> str | None:
        following = [c for p, c in code_positions if p >= pos]
        return following[0] if following else None

    article_positions = [(m.start(), m.group(1))
                         for m in _ARTICLE_MENTION.finditer(act)]

    def article_for(pos: int) -> str | None:
        following = [a for p, a in article_positions if p >= pos]
        if following:
            return following[0]
        prior = [a for p, a in article_positions if p < pos]
        return prior[-1] if prior else None

    for m in _STRUCT_MENTION.finditer(act):
        covered.append(m.span())
        verb, code = verb_for(m.start()), code_for(m.end())
        if verb and code:
            sec = m.group(3)
            if code == "CONS" and m.group(1) == "Article":
                sec = cons_key(m.group(2), sec)
            refs.append(Ref(verb, code, sec,
                            struct=f"{m.group(1)} {m.group(2)}"))
        else:
            unresolved += 1

    # Leg Counsel typo: "to amend 7928.205 of the Government Code" —
    # the word "Section" is missing before the number.
    _bare = re.compile(rf"\b{_VERB_WORDS}\s+({_SECNUM})\s+(?:of|to)\s+the\s+")
    section_mentions = list(_SECTION_MENTION.finditer(act))
    sec_spans = [m.span() for m in section_mentions]
    for m in _bare.finditer(act):
        if any(a <= m.start(1) < b for a, b in covered + sec_spans):
            continue
        verb, code = verb_for(m.start(1)), code_for(m.end())
        if verb and code and re.search(r"\d", m.group(1)):
            refs.append(Ref(verb, code, m.group(1).rstrip(".,")))
        else:
            unresolved += 1

    for m in section_mentions:
        if any(a <= m.start() < b for a, b in covered):
            continue  # part of a "(commencing with Section X)" struct
        after = act[m.end(): m.end() + 60]
        if _STATUTES_REF.match(after.lstrip()) or \
                re.match(r"\s+of\s+Chapter\s+\d", after):
            continue  # session-law citation, not a code section
        verb, code = verb_for(m.start()), code_for(m.end())
        sections = _split_seclist(m.group(1))
        if verb and code:
            for sec, is_range, end in sections:
                # Constitution refs get the canonical article-scoped key so
                # they join against law_section.section_num_norm directly.
                # Bare fallback when no article is named anywhere.
                if code == "CONS":
                    art = article_for(m.end())
                    secname = cons_key(art, sec) if art else sec
                else:
                    secname = sec
                refs.append(Ref(verb, code, secname, is_range, end))
        else:
            unresolved += len(sections)

    if refs and not unresolved:
        return ParseResult("ok", refs)
    if refs:
        return ParseResult("partial", refs, note=f"{unresolved} unresolved")
    if unresolved:
        return ParseResult("fail", note=f"{unresolved} unresolved mentions")
    if _CODE_RE.search(act) or "Section" in act:
        return ParseResult("fail", note="mentions present but nothing parsed")
    return ParseResult("no_sections", note="no section refs in act portion")

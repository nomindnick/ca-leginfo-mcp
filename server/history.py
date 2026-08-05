"""History-note citation parser for get_legislative_history (SPEC §5 tool 6).

A law_section.history note names the section's most recent legislative
event, in one of a handful of real forms (all observed in the built
corpus):

- "Amended by Stats. 2023, Ch. 260, Sec. 14.   (SB 345) ..."
- "Amended (as amended by Stats. 2021, Ch. 615, Sec. 208) by
   Stats. 2022, Ch. 971, Sec. 1.   (AB 2647) ..." — TWO chapter
  citations; both are returned, operative one first in note order.
- "Added by Stats. 1946, 1st Ex. Sess., Ch. 114." — extraordinary
  session; resolved via bill.chapter_session_num.
- "Added November 4, 2014, by initiative Proposition 47, Sec. 5." —
  voter initiative: no authoring bill exists.
- Constitution: "Sec. 1.1 added Nov. 8, 2022, by Prop. 1. Res.Ch. 97,
  2022." — the resolution chapter resolves (chapter_type='CHR') to the
  SCA/ACA that put the measure on the ballot.
- Constitution + ex. session: "... by Prop. 58. Res.Ch. 1, 2003-04
  5th Ex. Sess."
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class HistoryEvent:
    kind: str  # "chapter" | "resolution_chapter" | "initiative"
    citation: str
    year: int | None = None
    year_alt: int | None = None  # second year of a "2003-04" range
    chapter: int | None = None
    ex_session: int = 0  # 0 = regular session
    proposition: str | None = None
    date: str | None = None
    measure_hint: str | None = None
    # "operative" = the event this note records; "prior_version" = the
    # parenthetical "(as amended by …)" citation naming the text it acted on.
    role: str = "operative"

    def key(self) -> tuple:
        return (self.kind, self.year, self.chapter, self.ex_session,
                self.proposition)


@dataclass
class ParsedHistory:
    note: str
    events: list[HistoryEvent] = field(default_factory=list)


_EX_SESS = r"(\d{1,2})(?:st|nd|rd|th)\s+Ex(?:\.|traordinary)?\s*Sess\.?"

_STATS = re.compile(
    rf"Stats\.?\s*(\d{{4}})\s*,\s*(?:{_EX_SESS}\s*,\s*)?Ch\.?\s*(\d+)")

_RES_CH = re.compile(
    rf"Res(?:olution)?\s*\.?\s*Ch(?:apter)?\.?\s*(\d+)\s*,\s*"
    rf"(\d{{4}})(?:[-–](\d{{2,4}}))?(?:\s+{_EX_SESS})?")

_INITIATIVE = re.compile(
    r"by\s+initiative\s+(?:measure\s+)?Prop(?:osition|\.)?\s*"
    r"([0-9]+[A-Za-z-]*)", re.IGNORECASE)

_PROP = re.compile(r"by\s+Prop(?:osition|\.)?\s*([0-9]+[A-Za-z-]*)",
                   re.IGNORECASE)

_MEASURE_HINT = re.compile(r"\(\s*([A-Z]{2,4})\s*(\d+)\s*\)")

_DATE = re.compile(
    r"\b((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+"
    r"\d{1,2},\s+\d{4})")

# Any parenthetical that itself contains a Stats./Res.Ch. citation names
# the prior text the operative event acted on — "(as amended by …)",
# "(as added by …)", "(as renumbered … by …)" all follow this shape,
# while the operative citation is never parenthesized and the measure
# hint "(AB 2647)" contains no chapter citation.
_PRIOR_PAREN = re.compile(
    r"\([^)]*(?:Stats\.|Res(?:olution)?\s*\.?\s*Ch)[^)]*\)", re.IGNORECASE)

_INITIATIVE_MEASURE = re.compile(r"\bInitiative\s+measure\b", re.IGNORECASE)


def _last_before(regex: re.Pattern, note: str, pos: int) -> re.Match | None:
    """Nearest match of ``regex`` ending at or before ``pos`` — history
    notes name a citation's proposition/date immediately before it."""
    last = None
    for m in regex.finditer(note, 0, pos):
        last = m
    return last


def parse_history(note: str | None) -> ParsedHistory:
    """Extract every resolvable citation from a history note, in note
    order (the first is the operative/most-recent event)."""
    out = ParsedHistory(note=note or "")
    if not note:
        return out

    hint = _MEASURE_HINT.search(note)
    measure_hint = f"{hint.group(1)} {hint.group(2)}" if hint else None

    def date_before(pos: int) -> str | None:
        m = _last_before(_DATE, note, pos)
        return m.group(1) if m else None

    events: list[tuple[int, HistoryEvent]] = []

    for m in _STATS.finditer(note):
        events.append((m.start(), HistoryEvent(
            kind="chapter", citation=m.group(0),
            year=int(m.group(1)), chapter=int(m.group(3)),
            ex_session=int(m.group(2) or 0))))

    for m in _RES_CH.finditer(note):
        year = int(m.group(2))
        alt_raw = m.group(3)
        year_alt = None
        if alt_raw:
            year_alt = int(alt_raw) if len(alt_raw) == 4 else \
                year - year % 100 + int(alt_raw)
        # Multi-citation CONS notes ("… by Prop. 1. Res.Ch. 18, 1979.
        # Other Source: … by Prop. 7; Res.Ch. 90, 1974.") attribute each
        # Res.Ch. to the proposition/date named just before it.
        prop = _last_before(_PROP, note, m.start()) or _PROP.search(note)
        events.append((m.start(), HistoryEvent(
            kind="resolution_chapter", citation=m.group(0),
            year=year, year_alt=year_alt, chapter=int(m.group(1)),
            ex_session=int(m.group(4) or 0),
            proposition=prop.group(1) if prop else None,
            date=date_before(m.start()))))

    if not any(e.kind == "resolution_chapter" for _, e in events):
        # Codified-initiative form ("… by initiative Proposition 47") or
        # the Constitution's direct form ("… by Prop. 8. Initiative
        # measure." — no resolution chapter exists for those).
        m = _INITIATIVE.search(note)
        if not m and _INITIATIVE_MEASURE.search(note):
            m = _PROP.search(note)
        if m:
            events.append((m.start(), HistoryEvent(
                kind="initiative", citation=m.group(0),
                proposition=m.group(1), date=date_before(m.start()))))

    # Citations inside a "(… by Stats./Res.Ch. …)" parenthetical describe
    # the prior text the operative event acted on, not the event itself.
    paren_spans = [m.span() for m in _PRIOR_PAREN.finditer(note)]
    for pos, ev in events:
        if any(lo <= pos < hi for lo, hi in paren_spans):
            ev.role = "prior_version"

    # Operative events first (note order within each role).
    events.sort(key=lambda t: (t[1].role != "operative", t[0]))
    seen: set[tuple] = set()
    for pos, ev in events:
        if ev.key() in seen:
            continue
        seen.add(ev.key())
        out.events.append(ev)

    # The parenthetical measure ("(AB 2647)") names the operative bill.
    if measure_hint and out.events and out.events[0].kind == "chapter" \
            and out.events[0].role == "operative":
        out.events[0].measure_hint = measure_hint
    return out

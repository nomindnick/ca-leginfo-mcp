"""Tools 8–10 (SPEC §12): bill text serving and version comparison.

Plain functions over Databases, like server/tools.py — the MCP wiring
lives in server/app.py. Shares the seven tools' private helpers
(_find_bills, _chapter_bill, …) deliberately: both modules are one tool
surface over one envelope contract, and duplicating the bill-resolution
idioms would let them drift.

Design decisions carried from SPEC §12 and the V2 spike:

- get_bill_text serves a print whole up to MAX_FULL_TEXT chars (50k —
  ~91% of real 2023–24 prints; measured p90 43k). Over that it returns a
  SEC-block index (heading + intro line, the intro naming each block's
  target section) plus a note to re-query with section_filter — the
  trailer-bill navigation contract (decision D3).
- compare_section_versions resolves refs to text via the history-note
  chain: a section's note names the operative chapter AND its act-section
  number ("Stats. 2023, Ch. 534, Sec. 1"), and each print block's lineage
  parenthetical names its predecessor the same way. Sunset branches are
  the norm (§ 54953 carries four parallel blocks in AB 557), so the
  act-section hint picks the operative variant; when no hint reaches a
  chapter, the first block is compared and every other block is listed
  with its lineage — a loud pick, never a silent one.
- compare_bill_versions splits each print at the enacting clause and
  redlines title+digest separately from (and before) the body:
  Legislative Counsel's own summary of the change is signal, not noise.
- Identical endpoints return an affirmative no-change statement — the
  calling model must be able to assert sameness, not infer it from an
  empty diff.
- Every redline response instructs verbatim reproduction: models are
  unreliable at computing diffs, reliable at copying them.
"""

from __future__ import annotations

import difflib
import logging
import re
import sqlite3
import zlib
from contextlib import nullcontext
from dataclasses import asdict

from server import billtext, naming
from server import history as history_mod
from server.db import Databases, envelope
from server.history import _EX_SESS
from server.redline import Redline, _segments, redline
from server.tools import (
    _ambiguity_note,
    _bill_summary,
    _chapter_bill,
    _chapter_cite,
    _era_notes,
    _find_bills,
    _parse_section_or_error,
    _resolve_code_or_error,
    _section_suggestions,
)

log = logging.getLogger("server.texttools")

# Serve a print whole up to this many characters; above it, index mode.
# Tuned on the 2023–24 session: p90 = 43k chars, so ~91% of prints serve
# whole, and everything larger is exactly the omnibus/budget class the
# SEC index exists for.
MAX_FULL_TEXT = 50_000

# Whole-bill comparison guards. Size is NOT the cost driver — measured:
# a near-identical 1.8M-char budget-bill pair redlines in 0.3s, while a
# 6k→535k gut-and-amend takes 17s — the pairing pass inside replace
# ranges is. Nor is the CALL COUNT: each pairing call is a character-
# level SequenceMatcher over whole segments, and budget prints carry
# 100k+-char segments, so 27 calls once ran 219s (round-1 finding).
# _MAX_PAIR_WORK therefore caps the CHARACTER PRODUCT of replace
# ranges (chars_old × chars_new summed per range): the 219s pair
# measures ~4e10, so 1e9 bounds the worst admitted diff near ~5s.
# _MAX_REDLINE_CHARS caps the body redline served (a redline longer
# than a servable print is unreadable in chat) — the digest redline is
# always served either way.
_MAX_PAIR_WORK = 1_000_000_000
_MAX_REDLINE_CHARS = MAX_FULL_TEXT

# SEC-index intro lines are clipped here (SPEC §12: ≤200 chars).
_INTRO_CAP = 200

# History-note chain walk: how many lineage hops to follow before giving
# up on finding an act-section hint for a requested chapter.
_CHAIN_DEPTH = 25

VERBATIM_NOTE = (
    "Reproduce the redline markdown and the change list verbatim — "
    "*italics* = added, ~~strikeout~~ = deleted (the official bill-print "
    "convention). Do not recompute or paraphrase the diff: language "
    "models are unreliable at computing diffs and reliable at copying "
    "them.")

# Measure types that can appear in a version ref ("AB 405"); anything
# else ("Ch 534") must fall through to the chapter grammar.
_MEASURE_TYPES = frozenset({
    "AB", "SB", "ACA", "SCA", "ACR", "SCR", "AJR", "SJR", "HR", "SR"})

# "Stats. 2023, Ch. 534, Sec. 1" — the history-note citation form; the
# trailing act-section number is the variant hint (optional). Chapter
# digits are bounded ({1,5}): int() on an unbounded user-supplied digit
# run raises ValueError past 4300 digits (CPython 3.11+), and no real
# chapter needs more than five.
_STATS_SEC = re.compile(
    rf"Stats\.?\s*(\d{{4}})\s*,\s*(?:{_EX_SESS}\s*,\s*)?"
    rf"Ch\.?\s*(\d{{1,5}})(?!\d)(?:\s*,\s*Sec\.\s*([\d.]+?)\.?(?=\s|\)|,|$))?")

# "Section 1 of Chapter 534 of the Statutes of 2023" — the lineage-
# parenthetical form Legislative Counsel prints in bill intros.
_SEC_OF_CH = re.compile(
    r"(?:Section\s+([\d.]+)\s+of\s+)?Chapter\s+(\d{1,5})(?!\d)\s+of\s+the\s+"
    r"Statutes\s+of\s+(\d{4})", re.IGNORECASE)

# Loose chapter forms: "Ch. 534, 2023" / "2023 ch 534" / "chapter 534 of 2023".
_LOOSE_CH = re.compile(
    r"^(?:(\d{4})\s*[,/ ]\s*ch(?:apter)?\.?\s*(\d{1,5})"
    r"|ch(?:apter)?\.?\s*(\d{1,5})\s*(?:[,/ ]|of)\s*(\d{4}))\s*$",
    re.IGNORECASE)

_ENACTING_CLAUSE = re.compile(
    r"do\s+enact\s+as\s+follows\s*:", re.IGNORECASE)

# The act-section number inside a heading: "SEC. 1.5." -> "1.5". Must
# start at a digit or it would match the dot in "SEC." itself.
_HEAD_NUM = re.compile(r"\d[\d.]*")


class _NoVersionText(Exception):
    """bill_version_text is absent from this store (a pre-V2 current.db:
    the deployed artifact refreshes nightly, code can precede data)."""


class _CorruptText(Exception):
    """A stored text_zlib blob failed to decompress."""

    def __init__(self, version_id: str):
        self.version_id = version_id
        super().__init__(version_id)


def _corrupt_error(e: _CorruptText) -> dict:
    return {"error": f"The stored text for print {e.version_id} is "
                     "corrupt in this artifact and cannot be served."}


_NO_TEXT_ERROR = (
    "Bill version text is not available in this server's current.db yet "
    "(the artifact predates version-text storage and refreshes nightly; "
    "try again after the next nightly build).")


# ---------------------------------------------------------------------------
# shared version plumbing
# ---------------------------------------------------------------------------

def _load_text(con, version_id: str) -> tuple[str | None, str | None]:
    """(title, flattened text) for one print; (None, None) when the
    version has no stored text (no lob was published for it)."""
    try:
        row = con.execute(
            """SELECT title_text, text_zlib FROM bill_version_text
               WHERE bill_version_id=?""", (version_id,)).fetchone()
    except sqlite3.OperationalError as e:
        raise _NoVersionText from e
    if not row or row[1] is None:
        return None, None
    try:
        return row[0], zlib.decompress(row[1]).decode()
    except zlib.error as e:
        raise _CorruptText(version_id) from e


def _version_rows(con, bill_id: str) -> list[dict]:
    """All prints of a bill, newest first (Legislative Counsel version
    numbers count down from 99), each flagged for stored text."""
    try:
        rows = con.execute(
            """SELECT v.bill_version_id, v.version_num, v.action,
                      v.action_date, t.bill_version_id IS NOT NULL
               FROM bill_version v
               LEFT JOIN bill_version_text t
                 ON t.bill_version_id = v.bill_version_id
               WHERE v.bill_id=?
               ORDER BY CAST(v.version_num AS INTEGER)""",
            (bill_id,)).fetchall()
    except sqlite3.OperationalError as e:
        raise _NoVersionText from e
    return [{"version_id": vid, "version_num": vn, "action": a, "date": d,
             "has_text": bool(ht)} for vid, vn, a, d, ht in rows]


def _version_label(v: dict) -> str:
    date = (v["date"] or "")[:10]
    return f"{v['action']} ({date}, version {v['version_num']})"


def _pick_version(versions: list[dict], spec) -> tuple[dict | None,
                                                       str | None,
                                                       dict | None]:
    """Resolve a from_version/to_version argument against a bill's print
    list. Returns (version, warning, error) — exactly one of version or
    error is set. Accepts a bill_version_id, a version number, a date
    (YYYY-MM-DD), or an action phrase ("chaptered", "amended assembly");
    an ambiguous phrase resolves to the newest match with a warning
    naming the alternatives (the chapter_to_bill idiom)."""
    if spec is None:
        return versions[0], None, None
    s = str(spec).strip()
    for v in versions:
        if v["version_id"].upper() == s.upper().replace(" ", ""):
            return v, None, None
    if s.isdigit():
        hits = [v for v in versions if v["version_num"] == s]
        if len(hits) == 1:
            return hits[0], None, None
        if hits:
            # Duplicate version_num rows exist in the real archive
            # (pubinfo anomaly): same idiom as the phrase branch.
            return hits[0], (
                f"{len(hits)} prints carry version number {s}: "
                + "; ".join(_version_label(v) for v in hits)
                + f" — using {_version_label(hits[0])}. Pass a "
                  "bill_version_id to pick another."), None
    else:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
            hits = [v for v in versions
                    if (v["date"] or "").startswith(s)]
        else:
            hits = [v for v in versions
                    if s.casefold() in (v["action"] or "").casefold()]
        if len(hits) == 1:
            return hits[0], None, None
        if hits:
            return hits[0], (
                f"{len(hits)} prints match {s!r}: "
                + "; ".join(_version_label(v) for v in hits)
                + f" — using the newest, {_version_label(hits[0])}."), None
    return None, None, {
        "error": f"No print of this bill matches version {s!r}.",
        "available_versions": [_version_label(v) for v in versions],
    }


def _bill_store(dbs: Databases, con_current, db_label: str):
    """Context manager for the store a bill was found in."""
    if db_label == "archive":
        return dbs.archive()
    return nullcontext(con_current)


# ---------------------------------------------------------------------------
# SEC-block index (tool 8's oversize mode)
# ---------------------------------------------------------------------------

def _heading_num(heading: str) -> float | None:
    m = _HEAD_NUM.search(heading)
    if not m:
        return None
    try:
        return float(m.group(0).rstrip("."))
    except ValueError:
        return None


def _sec_index(flat: str, version_id: str) -> list[dict]:
    """heading + intro line (≤200 chars) per enacting-section block.
    Codified blocks show their intro sentence through the action verb —
    which names the target section — uncodified blocks (findings,
    appropriations) fall back to their first sentence."""
    entries = []
    prev_num = None
    for block in billtext.split_blocks(flat)[1:]:
        head = billtext._SEC_HEAD.match(block)
        heading = head.group(0) if head else ""
        action = billtext._ACTION.search(block[:billtext._HEAD_WINDOW])
        if action:
            intro = " ".join(block[:action.end()].split())
        else:
            flowed = " ".join(block[:billtext._HEAD_WINDOW].split())
            cut = flowed.find(". ", len(heading))
            intro = flowed[:cut + 1] if cut > 0 else flowed
        if len(intro) > _INTRO_CAP:
            intro = intro[:_INTRO_CAP - 1] + "…"
        num = _heading_num(heading)
        if num is not None and prev_num is not None and num <= prev_num:
            # Enacting sections are strictly increasing in a real print;
            # a repeat/regression means a literal "SEC. n" inside quoted
            # statutory text fooled the splitter (SPEC §13 residual risk).
            log.warning(
                "suspected SEC-split false positive in %s: heading %r "
                "follows %.6g", version_id, heading, prev_num)
        if num is not None:
            prev_num = num
        entries.append({"heading": heading, "intro": intro})
    return entries


# A trailing subdivision citation ("(a)", "(b)(1)") is below the level
# bills' enacting sections operate at — accept and ignore it.
_TRAILING_SUBD = r"(?:\s*\([a-zA-Z0-9]{1,3}\))*"


def _parse_section_filter(text: str) -> tuple[str | None, str | None,
                                              dict | None]:
    """'GOV 54953' / 'Gov. Code 54953' / 'Section 54953 of the Government
    Code' -> (code, section_key, None), or (None, None, error)."""
    s = str(text).strip()
    m = re.match(
        rf"^(?:§+|sec(?:tion)?\.?)?\s*([0-9][\w.]*){_TRAILING_SUBD}"
        rf"\s+of\s+(?:the\s+)?(.+)$",
        s, re.IGNORECASE)
    if m:
        code_part, sec_part = m.group(2), m.group(1)
    else:
        m = re.match(
            rf"^(.*?)[\s,]+(?:§+|sec(?:tion)?\.?)?\s*([0-9][\w.]*)"
            rf"{_TRAILING_SUBD}\s*$",
            s, re.IGNORECASE)
        if not m:
            return None, None, {
                "error": f"Could not parse section_filter {text!r}.",
                "expected_format": 'code + section, e.g. "GOV 54953" or '
                                   '"Section 54953 of the Government Code"',
            }
        code_part, sec_part = m.group(1), m.group(2)
    rc = naming.resolve_code(code_part)
    if not rc:
        return None, None, {
            "error": f"Unrecognized code name in section_filter: "
                     f"{code_part!r}.",
            "suggestions": naming.code_suggestions(code_part),
        }
    if rc == "CONS":
        return None, None, {
            "error": "section_filter cannot target the Constitution: "
                     "constitutional amendments ride resolution chapters "
                     "whose prints use a different citation form.",
        }
    return rc, naming.parse_section(rc, sec_part)[1], None


def _block_dict(b: billtext.SectionBlock) -> dict:
    out = {"heading": b.heading, "action": b.action, "intro": b.intro,
           "text": b.body}
    if b.lineage:
        out["lineage"] = b.lineage
    return out


# ---------------------------------------------------------------------------
# tool 8: get_bill_text
# ---------------------------------------------------------------------------

def get_bill_text(dbs: Databases, measure: str, session: str | None = None,
                  version: str | int | None = None,
                  section_filter: str | None = None) -> dict:
    with dbs.current() as con:
        db_label, rows, sy, err = _find_bills(dbs, con, measure, session)
        if err:
            return envelope(con, err)
        notes = []
        summary = _bill_summary(rows[0], live=db_label == "current")
        if len(rows) > 1:
            notes.append(
                f"{len(rows)} measures matched (regular and extraordinary "
                f"sessions); using {summary['bill_id']}.")

        with _bill_store(dbs, con, db_label) as store:
            try:
                versions = _version_rows(store, summary["bill_id"])
                if not versions:
                    return envelope(con, {
                        "error": f"{summary['measure']} has no recorded "
                                 "prints."}, notes)
                picked, warn, err = _pick_version(versions, version)
                if err:
                    return envelope(con, err, notes)
                if warn:
                    notes.append(warn)
                title, flat = _load_text(store, picked["version_id"])
            except _NoVersionText:
                return envelope(con, {"error": _NO_TEXT_ERROR}, notes)
            except _CorruptText as e:
                return envelope(con, _corrupt_error(e), notes)
            if flat is None:
                err = {"error": f"No text is stored for "
                                f"{_version_label(picked)}.",
                       "available_versions": [
                           _version_label(v) for v in versions
                           if v["has_text"]]}
                if db_label == "archive":
                    with dbs.archive() as arc:
                        err["coverage"] = _era_notes(arc, sy[:4])
                return envelope(con, err, notes)

            payload = {
                "measure": summary["measure"],
                "session": summary["session"],
                "version": {k: picked[k] for k in
                            ("version_id", "version_num", "action", "date")},
                "other_versions": [_version_label(v) for v in versions
                                   if v is not picked],
                "title": title,
            }

            if section_filter:
                rc, key, err = _parse_section_filter(section_filter)
                if err:
                    return envelope(con, err, notes)
                code_name = naming.CODE_ALIASES[rc][0]
                blocks = billtext.section_blocks(flat, code_name, key)
                if not blocks:
                    notes.append(
                        f"No enacting section of this print operates on "
                        f"{code_name} § {key}; the full index follows.")
                    return envelope(con, {
                        **payload,
                        "section_filter": f"{rc} {key}",
                        "blocks": [],
                        "sections_index": _sec_index(
                            flat, picked["version_id"]),
                    }, notes)
                if len(blocks) > 1:
                    notes.append(
                        f"{len(blocks)} blocks of this print operate on "
                        f"{code_name} § {key} — parallel sunset/operative-"
                        "date variants are the norm; each carries its "
                        "lineage parenthetical.")
                return envelope(con, {
                    **payload,
                    "section_filter": f"{rc} {key}",
                    "blocks": [_block_dict(b) for b in blocks],
                }, notes)

            if len(flat) > MAX_FULL_TEXT:
                index = _sec_index(flat, picked["version_id"])
                if not index:
                    # Long resolutions (committee assignments, memorials)
                    # have no enacting sections: an empty index with
                    # section_filter guidance would make the text
                    # unreachable through any argument (round-1
                    # finding). Serving whole is the only honest option.
                    notes.append(
                        f"This print is {len(flat):,} characters and has "
                        "no enacting sections to index (resolutions "
                        "don't amend code sections) — serving the full "
                        "text.")
                    return envelope(con, {**payload, "text": flat}, notes)
                notes.append(
                    f"This print is {len(flat):,} characters — over the "
                    f"{MAX_FULL_TEXT:,}-character serving limit — so this "
                    f"is an index of its {len(index)} enacting sections. "
                    "Each intro line names the code section the block "
                    "operates on; re-query with section_filter (code + "
                    'section, e.g. "GOV 54953") for full block text.')
                return envelope(con, {
                    **payload,
                    "text_chars": len(flat),
                    "sections_index": index,
                }, notes)

            return envelope(con, {**payload, "text": flat}, notes)


# ---------------------------------------------------------------------------
# tool 9: compare_section_versions
# ---------------------------------------------------------------------------

def _parse_ref(text) -> dict | None:
    """A version ref: 'current', a chapter citation, or a measure."""
    s = str(text).strip()
    if s.casefold() in ("current", "current law", "now"):
        return {"kind": "current"}
    m = _STATS_SEC.search(s)
    if m:
        return {"kind": "chapter", "year": int(m.group(1)),
                "chapter": int(m.group(3)), "ex": int(m.group(2) or 0),
                "hint": m.group(4)}
    m = _SEC_OF_CH.search(s)
    if m:
        return {"kind": "chapter", "year": int(m.group(3)),
                "chapter": int(m.group(2)), "ex": 0, "hint": m.group(1)}
    m = _LOOSE_CH.match(s)
    if m:
        year = m.group(1) or m.group(4)
        ch = m.group(2) or m.group(3)
        return {"kind": "chapter", "year": int(year), "chapter": int(ch),
                "ex": 0, "hint": None}
    pm = naming.parse_measure(s)
    if pm and pm["type"] in _MEASURE_TYPES:
        return {"kind": "measure", "measure": s}
    return None


_REF_FORMATS = ('a chapter citation ("Stats. 2023, Ch. 534"), "current", '
                'or a measure ("AB 405") for its pending-proposed text')


def _note_hints(note: str | None) -> dict[tuple[int, int, int], str]:
    """(year, chapter, ex_session) -> act-section number, for every
    chapter citation in a history note that carries one."""
    hints = {}
    for m in _STATS_SEC.finditer(note or ""):
        if m.group(4):
            hints[(int(m.group(1)), int(m.group(3)),
                   int(m.group(2) or 0))] = m.group(4)
    return hints


def _lineage_node(lineage: str | None) -> dict | None:
    """A block's lineage parenthetical -> its predecessor chapter node."""
    m = _SEC_OF_CH.search(lineage or "")
    if not m:
        return None
    return {"year": int(m.group(3)), "chapter": int(m.group(2)), "ex": 0,
            "hint": m.group(1)}


def _prior_citing_chapter(dbs: Databases, con_current, rc: str, key: str,
                          before: tuple[int, int, int]) -> dict | None:
    """The most recently CHAPTERED chapter strictly before `before`
    (year, chapter, ex_session) whose enacted bill's final title cites
    the section — the title-based lineage leg of SPEC §12's resolution
    chain, for the common case where neither the history note nor the
    print names the predecessor (lineage parentheticals only exist for
    parallel-version sections).

    Ordered by the chaptered print's action_date, NOT chapter number:
    extraordinary-session chapters are numbered independently of the
    regular session, so a same-year ex-session chapter with a small
    number can be months newer (round-1 finding: WIC 13600's true prior
    was 1989 1st Ex. Sess. Ch. 2, five weeks after regular Ch. 1123).
    (year, chapter) ordering remains only as the date-less fallback."""
    sql = """SELECT b.chapter_year, b.chapter_num, b.chapter_session_num,
                    v.action_date
             FROM bill_section_ref r
             JOIN bill b ON b.bill_id = r.bill_id
              AND r.bill_version_id = b.latest_bill_version_id
             LEFT JOIN bill_version v
               ON v.bill_version_id = b.latest_bill_version_id
             WHERE r.law_code=? AND r.section=?
               AND b.chapter_num IS NOT NULL AND b.chapter_type='CHP'"""
    rows = list(con_current.execute(sql, (rc, key)))
    if dbs.has_archive:
        with dbs.archive() as arc:
            rows.extend(arc.execute(sql, (rc, key)))
    cands: dict[tuple[int, int, int], str | None] = {}
    for cy, cn, cs, date in rows:
        if cy and cn:
            k = (int(cy), int(cn), int(cs or 0))
            cands[k] = cands.get(k) or date
    op_date = cands.get(tuple(before))
    y0, n0 = before[0], before[1]
    prior = None
    if op_date:
        dated = [(d, k) for k, d in cands.items() if d and d < op_date]
        if dated:
            # Same-date ties (two chapters signed the same day) break
            # deterministically on the (year, chapter, ex) tuple.
            _d, prior = max(dated)
        else:
            # Only date-less rows may fall back to number order — a
            # dated row that isn't earlier is genuinely not earlier.
            undated = [k for k, d in cands.items()
                       if not d and (k[0], k[1]) < (y0, n0)]
            prior = max(undated) if undated else None
    else:
        # The op chapter isn't in the citing index (or carries no
        # date): number order within the year is all we have.
        for k in sorted(cands):
            if (k[0], k[1]) < (y0, n0):
                prior = k
    if prior is None:
        return None
    return {"year": prior[0], "chapter": prior[1], "ex": prior[2],
            "hint": None}


def _resolve_chapter(dbs: Databases, con_current, year: int, chapter: int,
                     ex: int):
    """Chapter -> (bill_row, db_label, warning, error). Searches both
    stores like _lookup_chapter (December chapters land the calendar year
    before their session's nominal years — never route by year)."""
    if year < 1989:
        return None, None, None, {
            "error": f"Stats. {year}, Ch. {chapter} predates the "
                     "Legislature's electronic records (bill data begins "
                     "with the 1989-90 session). Consult the State "
                     "Archives or a legislative-intent service for this "
                     "era.",
            "resolution": "predates_electronic_records",
        }
    rows = _chapter_bill(con_current, year, chapter, "CHP", ex)
    label = "current"
    if not rows and dbs.has_archive:
        with dbs.archive() as arc:
            rows = _chapter_bill(arc, year, chapter, "CHP", ex)
        label = "archive"
    if not rows:
        where = ("either store" if dbs.has_archive
                 else "current.db (archive.db is not available on this "
                      "server)")
        return None, None, None, {
            "error": f"Stats. {year}, Ch. {chapter}"
                     f"{f' ({ex} Ex. Sess.)' if ex else ''} did not match "
                     f"any bill in {where}.",
        }
    return rows[0], label, _ambiguity_note(rows), None


def _chapter_endpoint(dbs: Databases, con, node: dict, rc: str,
                      section: str, hints: dict,
                      notes: list[str]) -> tuple[dict | None, dict | None]:
    """Resolve a chapter node to (endpoint, None) or (None, error).
    The endpoint carries the picked block's text; every unpicked block is
    listed with its lineage (never a silent pick)."""
    code_name = naming.CODE_ALIASES[rc][0]
    row, label, warn, err = _resolve_chapter(
        dbs, con, node["year"], node["chapter"], node["ex"])
    if err:
        return None, err
    if warn:
        notes.append(warn)
    summary = _bill_summary(row, live=False)
    vid = summary["latest_version_id"]
    with _bill_store(dbs, con, label) as store:
        try:
            _title, flat = _load_text(store, vid)
        except _NoVersionText:
            return None, {"error": _NO_TEXT_ERROR}
        except _CorruptText as e:
            return None, _corrupt_error(e)
    citation = _chapter_cite("CHP", str(node["year"]), str(node["chapter"]),
                             str(node["ex"]))
    if flat is None:
        return None, {
            "error": f"No print text is stored for {citation} "
                     f"({summary['measure']}, {summary['session']}).",
        }
    blocks = billtext.section_blocks(flat, code_name, section)
    if not blocks:
        return None, {
            "error": f"{citation} ({summary['measure']}) has no "
                     f"individually extractable enacting section for "
                     f"{code_name} § {section} — the print may operate "
                     "on it inside a multi-section block (\"Sections X "
                     "and Y are amended…\") or an added-structure block "
                     "(\"Chapter N (commencing with Section …) is "
                     "added…\"), which this tool cannot split. Read the "
                     f"print itself via get_bill_text "
                     f"(\"{summary['measure']}\", session "
                     f"\"{summary['session']}\").",
        }
    hint = node.get("hint") or hints.get(
        (node["year"], node["chapter"], node["ex"]))
    picked = None
    if hint:
        for b in blocks:
            m = _HEAD_NUM.search(b.heading)
            if m and m.group(0).rstrip(".") == hint:
                picked = b
                break
    if picked is None:
        picked = blocks[0]
        if len(blocks) > 1:
            others = "; ".join(
                f"{b.heading} ({b.lineage or b.action})"
                for b in blocks if b is not picked)
            notes.append(
                f"{citation} carries {len(blocks)} parallel blocks for "
                f"§ {section} (sunset/operative-date variants). "
                + (f"Act section {hint} was named by the history-note "
                   f"chain but matched no block heading; "
                   if hint else
                   "The history-note chain does not reach this chapter, "
                   "so the operative variant could not be established; ")
                + f"compared the first printed block, {picked.heading} "
                  f"({picked.lineage or picked.action}). Others: {others}.")
    elif len(blocks) > 1:
        others = "; ".join(
            f"{b.heading} ({b.lineage or b.action})"
            for b in blocks if b is not picked)
        notes.append(
            f"{citation} carries {len(blocks)} parallel blocks for "
            f"§ {section}; the history-note chain names act section "
            f"{hint}, so {picked.heading} is the operative variant. "
            f"Others: {others}.")
    endpoint = {
        "citation": citation,
        "measure": summary["measure"],
        "session": summary["session"],
        "version_id": vid,
        "block": picked.heading,
        "action": picked.action,
        "source": "chaptered print",
    }
    if picked.lineage:
        endpoint["lineage"] = picked.lineage
    if picked.action == "repealed":
        notes.append(f"{citation}'s block {picked.heading} repealed "
                     f"§ {section}; its text is empty.")
    return {**endpoint, "_text": picked.body,
            "_sibling_actions": [b.action for b in blocks],
            "_repealed_lineage": next(
                (b.lineage for b in blocks
                 if b.action == "repealed" and b.lineage), None)}, None


def _walk_hints(dbs: Databases, con, rc: str, section: str, start: dict,
                hints: dict, needed: set) -> None:
    """Follow the lineage chain from `start`, teaching `hints` the
    act-section number for each chapter passed, until every needed
    chapter is covered or the chain runs out."""
    node = dict(start)
    scratch: list[str] = []
    for _ in range(_CHAIN_DEPTH):
        if needed <= set(hints):
            return
        ep, err = _chapter_endpoint(dbs, con, node, rc, section, hints,
                                    scratch)
        if err or ep is None:
            return
        nxt = _lineage_node(ep.get("lineage"))
        if not nxt:
            return
        if nxt["hint"]:
            hints[(nxt["year"], nxt["chapter"], nxt["ex"])] = nxt["hint"]
        if (nxt["year"], nxt["chapter"]) == (node["year"], node["chapter"]):
            return  # self-citation would loop forever
        node = nxt


def _current_endpoint(con, rc: str, key: str,
                      notes: list[str]) -> tuple[dict | None, dict | None]:
    rows = con.execute(
        """SELECT section_num, history, content_text, active_flg
           FROM law_section WHERE law_code=? AND section_num_norm=?
           ORDER BY (active_flg='Y') DESC, rowid""", (rc, key)).fetchall()
    if not rows:
        return None, {
            "error": f"Section {key} not found in the "
                     f"{naming.CODE_ALIASES[rc][0]}.",
            "suggestions": _section_suggestions(con, rc, key),
        }
    if len(rows) > 1:
        tails = "; ".join(
            f'version {i + 1}: "…{" ".join((r[1] or "").split())[-90:]}"'
            for i, r in enumerate(rows[1:], start=1))
        notes.append(
            f"§ {key} has {len(rows)} simultaneous versions in current "
            "law (operative-date or competing-chapter branches). Compared "
            f'the first (history: "…{" ".join((rows[0][1] or "").split())[-90:]}"); '
            f"the other {'is' if len(rows) == 2 else 'are'}: {tails}. "
            "Pass a chapter citation to compare a specific branch.")
    _snum, history, text, _active = rows[0]
    return {
        "citation": "current law",
        "history_note": history,
        "source": "law section text",
        "_text": (text or "").strip(),
        "_history": history,
    }, None


def _measure_endpoint(dbs: Databases, con, ref: str, rc: str, section: str,
                      notes: list[str]) -> tuple[dict | None, dict | None]:
    """A measure ref (D2): the bill's latest print's proposed text for
    the section — current session (impact analysis, not history)."""
    code_name = naming.CODE_ALIASES[rc][0]
    db_label, rows, _sy, err = _find_bills(dbs, con, ref, None)
    if err:
        return None, err
    summary = _bill_summary(rows[0], live=db_label == "current")
    with _bill_store(dbs, con, db_label) as store:
        try:
            versions = _version_rows(store, summary["bill_id"])
            if not versions:
                return None, {"error": f"{summary['measure']} has no "
                                       "recorded prints."}
            picked, _warn, err = _pick_version(versions, None)
            if err:
                return None, err
            _title, flat = _load_text(store, picked["version_id"])
        except _NoVersionText:
            return None, {"error": _NO_TEXT_ERROR}
        except _CorruptText as e:
            return None, _corrupt_error(e)
    if flat is None:
        return None, {
            "error": f"No text is stored for {summary['measure']}'s "
                     f"latest print ({_version_label(picked)}).",
        }
    blocks = billtext.section_blocks(flat, code_name, section)
    if not blocks:
        return None, {
            "error": f"{summary['measure']}'s latest print "
                     f"({_version_label(picked)}) contains no enacting "
                     f"section operating on {code_name} § {section}.",
        }
    b = blocks[0]
    if len(blocks) > 1:
        others = "; ".join(f"{x.heading} ({x.lineage or x.action})"
                           for x in blocks[1:])
        notes.append(
            f"{summary['measure']}'s print carries {len(blocks)} parallel "
            f"blocks for § {section}; compared the first, {b.heading} "
            f"({b.lineage or b.action}). Others: {others}.")
    status = ("pending print" if summary["pending"] else
              f"print ({summary['status'] or 'archived'})")
    endpoint = {
        "citation": f"{summary['measure']}, {_version_label(picked)}",
        "measure": summary["measure"],
        "session": summary["session"],
        "version_id": picked["version_id"],
        "block": b.heading,
        "action": b.action,
        "source": status,
    }
    if b.lineage:
        endpoint["lineage"] = b.lineage
    if summary["pending"]:
        notes.append(
            f"{summary['measure']} is pending "
            f"({summary['status']}); the compared text is proposed, not "
            "law.")
    return {**endpoint, "_text": b.body}, None


def _strip(endpoint: dict) -> dict:
    return {k: v for k, v in endpoint.items() if not k.startswith("_")}


def _changes_out(r: Redline) -> list[dict]:
    out = []
    for c in r.changes:
        d = {k: v for k, v in asdict(c).items() if v}
        d["kind"] = c.kind
        out.append(d)
    return out


def compare_section_versions(dbs: Databases, code: str, section: str,
                             from_ref: str | None = None,
                             to_ref: str | None = None) -> dict:
    with dbs.current() as con:
        rc, err = _resolve_code_or_error(code)
        if err:
            return envelope(con, err)
        if rc == "CONS":
            return envelope(con, {
                "error": "Constitution version comparison is not "
                         "supported: constitutional amendments are "
                         "chaptered as resolution chapters whose prints "
                         "use a different citation form than statute "
                         "re-enactments. Use get_legislative_history for "
                         "the amendment chain and the Secretary of "
                         "State's records for historical text.",
            })
        _kind, key, err = _parse_section_or_error(con, rc, section)
        if err:
            return envelope(con, err)

        notes: list[str] = []

        to_spec = _parse_ref(to_ref) if to_ref is not None else \
            {"kind": "current"}
        if to_spec is None:
            return envelope(con, {
                "error": f"Could not parse to_ref {to_ref!r}.",
                "expected_format": _REF_FORMATS})
        if from_ref is not None:
            from_spec = _parse_ref(from_ref)
            if from_spec is None:
                return envelope(con, {
                    "error": f"Could not parse from_ref {from_ref!r}.",
                    "expected_format": _REF_FORMATS})
        elif to_spec["kind"] == "measure":
            # D2 default: current law vs. the pending-proposed text.
            from_spec = {"kind": "current"}
        else:
            from_spec = {"kind": "prior"}

        # The current-law row anchors the chain: its history note names
        # the operative chapter (with act-section hint) and, in the
        # parenthetical, the prior version it amended.
        current_ep, cur_err = _current_endpoint(con, rc, key, notes)
        hints: dict[tuple[int, int, int], str] = {}
        parsed_note = None
        if current_ep:
            hints = _note_hints(current_ep["_history"])
            parsed_note = history_mod.parse_history(current_ep["_history"])

        def op_event():
            if not parsed_note:
                return None
            for ev in parsed_note.events:
                if ev.kind == "chapter" and ev.role == "operative":
                    return ev
            return None

        def prior_node() -> tuple[dict | None, dict | None]:
            """The section's prior operative version, as a chapter node."""
            if cur_err:
                return None, cur_err
            for ev in parsed_note.events if parsed_note else []:
                if ev.kind == "chapter" and ev.role == "prior_version":
                    return {"year": ev.year, "chapter": ev.chapter,
                            "ex": ev.ex_session,
                            "hint": hints.get((ev.year, ev.chapter,
                                               ev.ex_session))}, None
            # No parenthetical: the operative chapter's own block names
            # its predecessor in its lineage parenthetical.
            ev = op_event()
            if ev is None:
                return None, {
                    "error": "The section's history note names no "
                             "chapter citation to walk back from; pass "
                             "from_ref explicitly (a chapter citation "
                             "from get_legislative_history).",
                    "history_notes": [current_ep["_history"]],
                }
            # A note that opens "Added by …" records the section's birth
            # — no block extraction needed (structural adds bury the
            # section inside an added-chapter block that can't be split;
            # the note's own verb is the authority). "Renumbered"
            # sections do have priors under their old numbers, so only
            # the plain form short-circuits.
            note_text = current_ep["_history"] or ""
            if re.match(r"\s*Added\b", note_text) and \
                    "renumber" not in note_text.lower():
                by = f"Stats. {ev.year}, Ch. {ev.chapter}"
                if ev.measure_hint:
                    by += f" ({ev.measure_hint})"
                return None, {
                    "no_prior_version": True,
                    "statement": f"{naming.CODE_ALIASES[rc][0]} § {key} "
                                 f"was added by {by}; no prior version "
                                 "exists to compare.",
                }
            node = {"year": ev.year, "chapter": ev.chapter,
                    "ex": ev.ex_session,
                    "hint": hints.get((ev.year, ev.chapter,
                                       ev.ex_session))}
            ep, err = _chapter_endpoint(dbs, con, node, rc, key, hints,
                                        notes)
            if err:
                return None, err
            return _prior_of_endpoint(ep, node, note_text)

        def _prior_of_endpoint(ep: dict, ep_node: dict,
                               note_text: str = "") -> tuple[dict | None,
                                                             dict | None]:
            """The predecessor of a resolved chapter endpoint: its
            block's lineage, else the citing-bills chain before it. An
            "added" block means no prior — UNLESS the same act repealed
            the old section (a repealed sibling block, or a history note
            opening "Repealed and added"): a repeal-and-add replaces a
            section that existed, and claiming otherwise falsifies the
            note's own record (round-1 finding, 5,146 such notes)."""
            readd = (re.match(r"\s*Repealed\b", note_text or "")
                     or "repealed" in ep.get("_sibling_actions", []))
            if ep["action"].startswith("added") and not readd:
                return None, {
                    "no_prior_version": True,
                    "statement": f"{naming.CODE_ALIASES[rc][0]} § {key} "
                                 f"was added by {ep['citation']} "
                                 f"({ep['measure']}); no prior version "
                                 "exists to compare.",
                }
            if ep["action"].startswith("added") and readd:
                notes.append(
                    f"§ {key} was repealed and re-added by "
                    f"{ep['citation']}; the prior version is the "
                    "pre-repeal text.")
                # The repealed sibling block's own lineage parenthetical
                # names exactly the version that was repealed — the
                # act's citation beats the title-based guess.
                nxt = _lineage_node(ep.get("_repealed_lineage"))
                if nxt:
                    return nxt, None
            nxt = _lineage_node(ep.get("lineage"))
            if nxt:
                return nxt, None
            # The ordinary case: a single-version section's print names
            # no lineage. Fall back to the title-based citing-bills
            # chain (SPEC §12's third resolution leg).
            nxt = _prior_citing_chapter(
                dbs, con, rc, key,
                (ep_node["year"], ep_node["chapter"], ep_node["ex"]))
            if nxt:
                cite = _chapter_cite("CHP", str(nxt["year"]),
                                     str(nxt["chapter"]), str(nxt["ex"]))
                notes.append(
                    f"The prior version was located through enacted "
                    f"bills citing § {key} (title-based lineage, "
                    f"1989-present): {cite} is the most recently "
                    f"chaptered enactment before {ep['citation']} to "
                    "touch the section.")
                return nxt, None
            # Nothing after 1989 touched the section before the
            # operative chapter — the prior version almost certainly
            # predates electronic records (SPEC §12's § 84308 case),
            # unless it was only ever reached through range/structural
            # amendments the title index can't see.
            return None, {
                "resolution": "prior_version_untraceable",
                "error": f"{ep['citation']} amended § {key}, but its "
                         "print does not name the version it amended "
                         "and no earlier enacted bill (1989-present) "
                         "cites the section individually. The prior "
                         "version most likely predates the "
                         "Legislature's electronic records; it may "
                         "instead have been touched only inside a "
                         "range or structural amendment. See "
                         "get_legislative_history, or pass from_ref "
                         "explicitly.",
            }

        def resolve(spec) -> tuple[dict | None, dict | None]:
            if spec["kind"] == "current":
                return (current_ep, cur_err)
            if spec["kind"] == "measure":
                return _measure_endpoint(dbs, con, spec["measure"], rc,
                                         key, notes)
            if spec["kind"] == "prior":
                node, err = prior_node()
                if err:
                    return None, err
                return _chapter_endpoint(dbs, con, node, rc, key, hints,
                                         notes)
            # chapter: if the note itself doesn't hint this chapter,
            # walk the lineage chain from the operative event toward it.
            chkey = (spec["year"], spec["chapter"], spec["ex"])
            if not spec.get("hint") and chkey not in hints:
                ev = op_event()
                if ev:
                    _walk_hints(dbs, con, rc, key,
                                {"year": ev.year, "chapter": ev.chapter,
                                 "ex": ev.ex_session,
                                 "hint": hints.get((ev.year, ev.chapter,
                                                    ev.ex_session))},
                                hints, {chkey})
            return _chapter_endpoint(dbs, con, spec, rc, key, hints, notes)

        to_ep, err = resolve(to_spec)
        if err:
            return envelope(con, err, notes)
        if from_spec["kind"] == "prior" and to_spec["kind"] == "chapter":
            # "What did this chapter change?" — the omitted from_ref is
            # the version THAT chapter amended, not the prior of current
            # law (which is typically newer and would silently render
            # the redline back-to-front — round-1 finding).
            node, err = _prior_of_endpoint(to_ep, to_spec)
            if err:
                return envelope(con, err, notes)
            from_ep, err = _chapter_endpoint(dbs, con, node, rc, key,
                                             hints, notes)
        else:
            from_ep, err = resolve(from_spec)
        if err:
            return envelope(con, err, notes)

        if (from_spec["kind"] == "chapter" and to_spec["kind"] == "chapter"
                and (from_spec["year"], from_spec["chapter"]) >
                    (to_spec["year"], to_spec["chapter"])):
            notes.append(
                "from_ref cites a later chapter than to_ref; the "
                "redline reads back-to-front as requested (earlier "
                "text appears as the additions).")

        r = redline(from_ep["_text"], to_ep["_text"])
        payload = {
            "code": rc,
            "code_name": naming.CODE_ALIASES[rc][0],
            "section": key,
            "from": _strip(from_ep),
            "to": _strip(to_ep),
            "identical": r.identical,
        }
        if r.identical:
            payload["statement"] = (
                f"No textual change: {naming.CODE_ALIASES[rc][0]} § {key} "
                f"is identical between {from_ep['citation']} and "
                f"{to_ep['citation']}.")
        else:
            notes.insert(0, VERBATIM_NOTE)
            payload["redline_markdown"] = r.markdown
            payload["changes"] = _changes_out(r)
        return envelope(con, payload, notes)


# ---------------------------------------------------------------------------
# tool 10: compare_bill_versions
# ---------------------------------------------------------------------------

def _split_at_enacting_clause(flat: str) -> tuple[str, str]:
    """(title + digest, body). Bills carry the constitutional enacting
    clause; resolutions don't — then everything is 'body'."""
    m = _ENACTING_CLAUSE.search(flat)
    if not m:
        return "", flat
    return flat[:m.end()], flat[m.end():]


def _pair_work(old: str, new: str) -> int:
    """Estimated character workload of the redline's pairing pass: the
    product of the two sides' character counts inside each top-level
    replace range (each old segment is ratio()'d against each new
    segment, and ratio() is superlinear in segment length — counting
    calls alone let a 27-call budget-bill pair run 219s)."""
    A, B = _segments(old), _segments(new)
    sm = difflib.SequenceMatcher(a=A, b=B, autojunk=False)
    return sum(sum(len(s) for s in A[i1:i2]) * sum(len(s) for s in B[j1:j2])
               for tag, i1, i2, j1, j2 in sm.get_opcodes()
               if tag == "replace")


def _min_redline_chars(old: str, new: str) -> int:
    """Cheap lower bound on the redline's size: quick_ratio bounds
    similarity from above on character counts alone (O(n), no diff), so
    (1 - quick_ratio) x total bounds the changed content from below —
    a pair provably over the serving cap is refused before any diff
    work is spent on it."""
    sm = difflib.SequenceMatcher(a=old, b=new, autojunk=False)
    return int((len(old) + len(new)) * (1 - sm.quick_ratio()))


def _refused_body(reason: str, guidance_measure: str) -> dict:
    return {
        "identical": False,
        "unavailable": True,
        "statement": (
            f"{reason} The title_and_digest part above is served in "
            "full — Legislative Counsel's digest is its own summary of "
            "what changed. For statutory detail, run get_bill_text on "
            f"each print of {guidance_measure} (its sections_index names "
            "every target section) and compare_section_versions on the "
            "code sections that matter."),
    }


def _part(old: str, new: str) -> dict:
    r = redline(old, new)
    out = {"identical": r.identical}
    if r.identical:
        out["statement"] = "No textual change in this part."
    else:
        out["redline_markdown"] = r.markdown
        out["changes"] = _changes_out(r)
    return out


def compare_bill_versions(dbs: Databases, measure: str,
                          session: str | None = None,
                          from_version: str | int | None = None,
                          to_version: str | int | None = None) -> dict:
    with dbs.current() as con:
        db_label, rows, sy, err = _find_bills(dbs, con, measure, session)
        if err:
            return envelope(con, err)
        notes: list[str] = []
        summary = _bill_summary(rows[0], live=db_label == "current")
        if len(rows) > 1:
            notes.append(
                f"{len(rows)} measures matched (regular and extraordinary "
                f"sessions); using {summary['bill_id']}.")

        era: list[str] = []
        if db_label == "archive":
            with dbs.archive() as arc:
                era = _era_notes(arc, sy[:4])

        with _bill_store(dbs, con, db_label) as store:
            try:
                versions = _version_rows(store, summary["bill_id"])
            except _NoVersionText:
                return envelope(con, {"error": _NO_TEXT_ERROR}, notes)
            if not versions:
                return envelope(con, {
                    "error": f"{summary['measure']} has no recorded "
                             "prints."}, notes)

            to_v, warn, err = _pick_version(versions, to_version)
            if err:
                return envelope(con, err, notes)
            if warn:
                notes.append(warn)
            if from_version is None:
                idx = versions.index(to_v)
                if idx + 1 >= len(versions):
                    texted = [v for v in versions if v["has_text"]]
                    return envelope(con, {
                        "error": f"{summary['measure']} has no print "
                                 f"before {_version_label(to_v)} to "
                                 "compare against.",
                        "available_versions": [
                            _version_label(v) for v in texted],
                        **({"coverage": era} if era else {}),
                    }, notes)
                from_v = versions[idx + 1]
            else:
                from_v, warn, err = _pick_version(versions, from_version)
                if err:
                    return envelope(con, err, notes)
                if warn:
                    notes.append(warn)

            texts = {}
            for v in (from_v, to_v):
                try:
                    _title, flat = _load_text(store, v["version_id"])
                except _NoVersionText:
                    return envelope(con, {"error": _NO_TEXT_ERROR}, notes)
                except _CorruptText as e:
                    return envelope(con, _corrupt_error(e), notes)
                if flat is None:
                    err = {"error": f"No text is stored for "
                                    f"{_version_label(v)}.",
                           "available_versions": [
                               _version_label(x) for x in versions
                               if x["has_text"]]}
                    if int(sy[:4]) < 1999:
                        err["coverage"] = [
                            ("Pre-1999 sessions are archived chaptered-"
                             "only: intermediate prints were never "
                             "published electronically, so bill-version "
                             "comparison is unavailable for them."),
                            *era]
                    elif era:
                        err["coverage"] = era
                    return envelope(con, err, notes)
                texts[v["version_id"]] = flat

        old, new = texts[from_v["version_id"]], texts[to_v["version_id"]]
        vnum = int(from_v["version_num"])
        if vnum < int(to_v["version_num"]):
            notes.append(
                "from_version is a later print than to_version; the "
                "redline reads back-to-front as requested (Legislative "
                "Counsel version numbers count down from 99).")

        old_head, old_body = _split_at_enacting_clause(old)
        new_head, new_body = _split_at_enacting_clause(new)
        if not old_head and not new_head:
            notes.append(
                "No enacting clause found (resolutions and "
                "constitutional amendments carry none) — the whole print "
                "is compared as one part under 'body'.")
            head_part = {"identical": True,
                         "statement": "Not applicable: this print form "
                                      "has no title/digest part."}
        else:
            head_part = _part(old_head, new_head)

        if old_body == new_body:
            body_part = _part(old_body, new_body)
        elif (bound := _min_redline_chars(
                old_body, new_body)) > _MAX_REDLINE_CHARS:
            body_part = _refused_body(
                f"At least ~{bound:,} characters of these prints' bodies "
                f"differ — over the {_MAX_REDLINE_CHARS:,}-character "
                "redline serving limit (the prints share too little "
                "text).", summary["measure"])
        elif (work := _pair_work(old_body, new_body)) > _MAX_PAIR_WORK:
            # A wholesale rewrite of a large print (gut-and-amend, budget
            # omnibus): the pairing pass alone would run far past
            # interactive speed, and the redline would be print-sized.
            body_part = _refused_body(
                f"These prints' bodies are too extensively rewritten to "
                f"redline interactively (estimated pairing work "
                f"{work:,}, limit {_MAX_PAIR_WORK:,}).",
                summary["measure"])
        else:
            body_part = _part(old_body, new_body)
            md = body_part.get("redline_markdown", "")
            if len(md) > _MAX_REDLINE_CHARS:
                # The markdown reproduces the whole print with marks, so
                # a giant bill with modest edits busts the cap even
                # though its CHANGES are small — and the change list is
                # precisely the useful part then. Serve it when it fits.
                changes = body_part.get("changes", [])
                counts: dict[str, int] = {}
                for c in changes:
                    counts[c["kind"]] = counts.get(c["kind"], 0) + 1
                changed_chars = sum(
                    len(c.get("deleted") or "") + len(c.get("added") or "")
                    for c in changes)
                body_part = {
                    **_refused_body(
                        f"The body redline runs {len(md):,} characters — "
                        f"over the {_MAX_REDLINE_CHARS:,}-character "
                        "serving limit.",
                        summary["measure"]),
                    "change_counts": counts,
                }
                if changed_chars <= 30_000:
                    body_part["changes"] = changes
                    body_part["statement"] += (
                        " The structured change list itself is small and "
                        "is included in full under 'changes'.")
        identical = head_part["identical"] and body_part["identical"]
        payload = {
            "measure": summary["measure"],
            "session": summary["session"],
            "from": {k: from_v[k] for k in
                     ("version_id", "version_num", "action", "date")},
            "to": {k: to_v[k] for k in
                   ("version_id", "version_num", "action", "date")},
            "identical": identical,
            # Digest edits first: Legislative Counsel's own summary of
            # what changed is signal, not noise (SPEC §12).
            "title_and_digest": head_part,
            "body": body_part,
        }
        if identical:
            payload["statement"] = (
                f"No textual change: {summary['measure']}'s "
                f"{_version_label(from_v)} and {_version_label(to_v)} "
                "prints are identical.")
        else:
            notes.insert(0, VERBATIM_NOTE)
        if era:
            notes.extend(era)
        return envelope(con, payload, notes)

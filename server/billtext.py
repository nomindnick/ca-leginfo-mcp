"""SEC-block extraction from flattened bill text (SPEC §10, §13).

Every chaptered bill re-enacts amended sections at length (Cal. Const.
art. IV, § 9), so a code section's historical text is recoverable from
the flattened bill-version text archive.db / current.db already store —
no new source data, no precompute.

Flattening traps this module absorbs (SPEC §13, all observed in real
lobs):

- The first enacting section prints `SECTION 1.` (no dot after SECTION),
  the rest `SEC. 2.` — both forms split and match.
- Flattened lobs do not reliably newline before headings
  (`…do enact as follows:SECTION 1.Section 54953…`), so a heading
  directly after `.` or `:` splits too — and a block amending a
  *structural heading* ends with the unterminated new title, gluing the
  next `SEC. n.` to arbitrary text, so a heading followed by a genuine
  intro shape splits at any position. Residual risk: a literal "SEC. n"
  inside quoted statutory text would mis-split; the recorded robust
  upgrade is XML-side extraction (`caml:BillSection` +
  `caml:ActionLine`), which needs source zips rather than the DBs.
- Sunset/operative-date branches are the norm: one bill can carry
  several blocks for the same section (§ 54953 had four in AB 557).
  Callers get every matching block, each with its lineage parenthetical
  ("as amended by Section 2 of Chapter 285 of the Statutes of 2022") —
  the machine-readable key for ordering the version graph. Lineage
  citations are literal; double-jointed prints are *contingent* (AB
  557's `SEC. 1.5.` never became operative — SB 537 failed its
  condition — so "Section 1 of Chapter 534" cites `SECTION 1.`), and
  picking the operative block follows the history-note chain.
- `repealed` blocks carry no body.

Contract fixtures: tests/fixtures/sec54953/ (see its README), pinned by
tests/test_billtext.py.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Split before enacting-section headings; see module docstring for the
# two heading forms and the missing-newline trap. Second alternative:
# a block amending a *structural heading* ends with the new heading
# title, which has no terminal punctuation ("…is amended to
# read:6.4.COUNTY HEALTH INITIATIVE MATCHING FUNDSEC. 33.Section
# 12699.50…"), so a heading glued to arbitrary text must split too —
# but only when followed by a genuine intro shape ("Section N …" /
# "The heading of"), or quoted "SEC. n" in body text would mis-split.
_SEC_SPLIT = re.compile(
    r"(?:\n|(?<=[.:]))(?=SEC\.\s*\d|SECTION\s+\d)"
    r"|(?=(?:SEC\.|SECTION)\s*\d[\d.]*\s*"
    r"(?:Section\s+\d|The\s+heading\s+of\s))")
_SEC_HEAD = re.compile(r"(?:SEC\.|SECTION)\s*[\d.]+")

# The intro sentence's operative verb phrase: "… is amended to read:",
# "… is amended and renumbered Section X of … to read:", "… is added to
# the … Code, … to read:", "… is repealed."
_ACTION = re.compile(
    r"\b(?:is|are)\s+(?:further\s+)?"
    r"(amended(?:\s+and\s+renumbered)?[^:]{0,120}?to\s+read|"
    r"added[^:]{0,120}?to\s+read|repealed)\s*:?")

_LINEAGE = re.compile(r",\s*(as\s+(?:amended|added)\s+by[^,]{0,200}?),?\s+is")

# Intro sentences run at most a few hundred characters; the action verb
# is searched only inside this head window so a phrase like "is amended
# to read" deep in re-enacted body text cannot masquerade as an intro.
_HEAD_WINDOW = 600


@dataclass
class SectionBlock:
    """One enacting-section block of a bill that operates on the target
    code section."""
    heading: str        # "SECTION 1." / "SEC. 1.5." / "SEC. 89."
    action: str         # "amended … to read" / "added … to read" / "repealed"
    lineage: str | None  # "as amended by Section 2 of Chapter 285 of …"
    intro: str          # full intro sentence through the action verb
    body: str           # re-enacted section text; empty for repealed blocks


def split_blocks(flat_text: str) -> list[str]:
    """Raw enacting-section blocks of a flattened bill lob. The first
    element is the pre-enactment prefix (title, digest, enacting clause).

    A heading that is both newline-preceded and glued to a genuine intro
    shape satisfies two _SEC_SPLIT alternatives at adjacent positions
    (the ``\\n`` branch consumes, then the zero-width branch fires), so
    the raw split yields an empty string between them; those are noise,
    never content, and are dropped (the prefix is kept even when empty
    so callers can rely on its position)."""
    parts = _SEC_SPLIT.split(flat_text)
    return [parts[0]] + [p for p in parts[1:] if p]


def section_blocks(flat_text: str, code_name: str,
                   section: str) -> list[SectionBlock]:
    """Every block of a flattened bill lob that operates on `section` of
    `code_name` (display name, e.g. "Government Code"), in print order."""
    target = re.compile(
        rf"Section\s+{re.escape(section)}(?!\.?\d)\s+(?:of|is\s+added\s+to)"
        rf"\s+the\s+{re.escape(code_name)}")
    hits: list[SectionBlock] = []
    for block in split_blocks(flat_text):
        action = _ACTION.search(block[:_HEAD_WINDOW])
        if not action:
            continue
        # The target (and its lineage parenthetical) must be cited in
        # the intro sentence itself — before the action verb ends — not
        # merely near the top of the block: re-enacted bodies routinely
        # open "Notwithstanding Section N of the X Code…", and matching
        # that would deliver a different statute's text as a version of
        # the target.
        intro_region = block[:action.end()]
        if not target.search(intro_region):
            continue
        heading = _SEC_HEAD.match(block)
        lineage = _LINEAGE.search(intro_region)
        if action.group(1) == "repealed":
            body = ""  # nothing is re-enacted; only "is repealed." remains
        else:
            body = block[action.end():].lstrip("\n ")
            body = re.sub(rf"^{re.escape(section)}\.\s*", "", body)
        hits.append(SectionBlock(
            heading=heading.group(0) if heading else "",
            action=action.group(1),
            lineage=" ".join(lineage.group(1).split()) if lineage else None,
            intro=" ".join(block[:action.end()].split()),
            body=body.strip()))
    return hits

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
  directly after `.` or `:` splits too. Residual risk: a literal
  "SEC. n" inside quoted statutory text would mis-split; the recorded
  robust upgrade is XML-side extraction (`caml:BillSection` +
  `caml:ActionLine`), which needs source zips rather than the DBs.
- Sunset/operative-date branches are the norm: one bill can carry
  several blocks for the same section (§ 54953 had four in AB 557).
  Callers get every matching block, each with its lineage parenthetical
  ("as amended by Section 2 of Chapter 285 of the Statutes of 2022") —
  the machine-readable key for ordering the version graph. Lineage
  matching needs double-jointing tolerance: later bills cite
  "Section 1 of Chapter 534" for the block that printed as `SEC. 1.5.`.
- `repealed` blocks carry no body.

Contract fixtures: tests/fixtures/sec54953/ (see its README), pinned by
tests/test_billtext.py.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Split before enacting-section headings; see module docstring for the
# two heading forms and the missing-newline trap.
_SEC_SPLIT = re.compile(r"(?:\n|(?<=[.:]))(?=SEC\.\s*\d|SECTION\s+\d)")
_SEC_HEAD = re.compile(r"(?:SEC\.|SECTION)\s*[\d.]+")

# The intro sentence's operative verb phrase: "… is amended to read:",
# "… is amended and renumbered Section X of … to read:", "… is added to
# the … Code, … to read:", "… is repealed."
_ACTION = re.compile(
    r"\b(?:is|are)\s+(?:further\s+)?"
    r"(amended(?:\s+and\s+renumbered)?[^:]{0,120}?to\s+read|"
    r"added[^:]{0,120}?to\s+read|repealed)\s*:?")

_LINEAGE = re.compile(r",\s*(as\s+(?:amended|added)\s+by[^,]{0,200}?),?\s+is")

# Intro sentences run at most a few hundred characters; matching the
# target inside the block head avoids false hits on "Section N" citations
# deep in re-enacted body text.
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
    element is the pre-enactment prefix (title, digest, enacting clause)."""
    return _SEC_SPLIT.split(flat_text)


def section_blocks(flat_text: str, code_name: str,
                   section: str) -> list[SectionBlock]:
    """Every block of a flattened bill lob that operates on `section` of
    `code_name` (display name, e.g. "Government Code"), in print order."""
    target = re.compile(
        rf"Section\s+{re.escape(section)}(?!\.?\d)\s+(?:of|is\s+added\s+to)"
        rf"\s+the\s+{re.escape(code_name)}")
    hits: list[SectionBlock] = []
    for block in split_blocks(flat_text):
        head = block[:_HEAD_WINDOW]
        if not target.search(head):
            continue
        action = _ACTION.search(head)
        if not action:
            continue
        heading = _SEC_HEAD.match(block)
        lineage = _LINEAGE.search(head)
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

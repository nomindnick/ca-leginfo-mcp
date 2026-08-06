"""Subdivision-anchored redline engine (SPEC §10).

Produces a display-ready markdown redline between two versions of legal
text — `*italics*` = added, `~~strikeout~~` = deleted, the official
California bill-print convention — plus a structured change list.

Why not a flat word-level diff: on wholesale rewrites SequenceMatcher
anchors on incidental shared words and interleaves unrelated provisions
(the spike produced hunks like `~~meeting during~~ *physical disability
or*`). Legal redlines need structure first:

1. Tokenize whitespace-insensitively — sources differ in layout
   (chaptered-bill lobs vs. law lobs), never in words; flowed words
   yield zero phantom hunks across sources.
2. Segment both versions before subdivision markers (`(a)`, `(1)`,
   `(A)`, `(i)`) that follow sentence-ending punctuation. Content-derived,
   so both sources segment identically despite layout noise.
3. Align segments; inside `replace` ranges, pair segments by similarity
   ratio (floor 0.5, order-preserving greedy). Unpaired segments render
   as whole-provision strikeout/insert.
4. Word-diff only within paired segments; absorb equal runs of ≤2 tokens
   between adjacent changes (the matcher anchoring on a stray "of" would
   otherwise split one logical replacement into three hunks).

Contract fixtures: the Gov. Code § 54953 version chain in
tests/fixtures/sec54953/ (sunset branches, double-jointing, a wholesale
rewrite) with golden outputs pinned by tests/test_redline.py.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field

_WORD = re.compile(r"\S+")

# Subdivision markers following sentence-ending punctuation start a new
# segment: "… of this chapter. (b) Notwithstanding …" splits before "(b)".
_SEG_MARK = re.compile(r"(?<=[.:;])\s+(?=\([a-zA-Z0-9]{1,4}\)\s)")

# Below this similarity ratio, replaced segments are unrelated provisions:
# render delete + insert, not a word-level edit.
_PAIR_FLOOR = 0.5

# Change-list context window, in tokens of the old text.
_CONTEXT = 6


@dataclass
class Change:
    """One entry of the structured change list.

    kind: "edit" (word-level change within a paired segment),
    "new_provision" / "deleted_provision" (whole unpaired segment —
    the provision text is self-locating, so no context is attached).
    """
    kind: str
    deleted: str | None = None
    added: str | None = None
    context_before: str = ""
    context_after: str = ""


@dataclass
class Redline:
    markdown: str
    changes: list[Change] = field(default_factory=list)

    @property
    def identical(self) -> bool:
        """True when the versions carry no textual change — tools must
        assert sameness affirmatively, never leave it implied (SPEC §12)."""
        return not self.changes


def _tokens(text: str) -> list[str]:
    return _WORD.findall(text)


def _segments(text: str) -> list[str]:
    return _SEG_MARK.split(" ".join(text.split()))


def _merge_ops(ops: list) -> list:
    """Absorb equal runs of ≤2 tokens sandwiched between changes, then
    coalesce the adjacent changes into one replace hunk."""
    merged: list = []
    for idx, (tag, i1, i2, j1, j2) in enumerate(ops):
        if (tag == "equal" and i2 - i1 <= 2
                and merged and merged[-1][0] != "equal"
                and idx + 1 < len(ops) and ops[idx + 1][0] != "equal"):
            tag = "replace"
        if merged and merged[-1][0] != "equal" and tag != "equal":
            p = merged[-1]
            merged[-1] = ("replace", p[1], i2, p[3], j2)
        else:
            merged.append((tag, i1, i2, j1, j2))
    return merged


def _word_diff(old: str, new: str) -> tuple[str, list[Change]]:
    a, b = _tokens(old), _tokens(new)
    sm = difflib.SequenceMatcher(a=a, b=b, autojunk=False)
    out: list[str] = []
    changes: list[Change] = []
    for tag, i1, i2, j1, j2 in _merge_ops(sm.get_opcodes()):
        if tag == "equal":
            out.extend(a[i1:i2])
            continue
        deleted, added = " ".join(a[i1:i2]), " ".join(b[j1:j2])
        if deleted:
            out.append(f"~~{deleted}~~")
        if added:
            out.append(f"*{added}*")
        changes.append(Change(
            kind="edit",
            deleted=deleted or None,
            added=added or None,
            context_before=" ".join(a[max(0, i1 - _CONTEXT):i1]),
            context_after=" ".join(a[i2:i2 + _CONTEXT])))
    return " ".join(out), changes


def _pair(olds: list[str],
          news: list[str]) -> list[tuple[int | None, int | None]]:
    """Order-preserving greedy pairing of replaced segments by ratio."""
    pairs: list[tuple[int | None, int | None]] = []
    j0 = 0
    for i, o in enumerate(olds):
        best, best_j = _PAIR_FLOOR, None
        for j in range(j0, len(news)):
            r = difflib.SequenceMatcher(None, o, news[j],
                                        autojunk=False).ratio()
            if r > best:
                best, best_j = r, j
        if best_j is None:
            pairs.append((i, None))
        else:
            for j in range(j0, best_j):
                pairs.append((None, j))
            pairs.append((i, best_j))
            j0 = best_j + 1
    pairs.extend((None, j) for j in range(j0, len(news)))
    return pairs


def redline(old: str, new: str) -> Redline:
    """Markdown redline of two statute/bill texts, one line per segment,
    plus the structured change list. Whitespace-insensitive throughout."""
    A, B = _segments(old), _segments(new)
    sm = difflib.SequenceMatcher(a=A, b=B, autojunk=False)
    lines: list[str] = []
    changes: list[Change] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            lines.extend(A[i1:i2])
        elif tag == "delete":
            for s in A[i1:i2]:
                lines.append(f"~~{s}~~")
                changes.append(Change(kind="deleted_provision", deleted=s))
        elif tag == "insert":
            for s in B[j1:j2]:
                lines.append(f"*{s}*")
                changes.append(Change(kind="new_provision", added=s))
        else:
            olds, news = A[i1:i2], B[j1:j2]
            for oi, nj in _pair(olds, news):
                if nj is None:
                    lines.append(f"~~{olds[oi]}~~")
                    changes.append(Change(kind="deleted_provision",
                                          deleted=olds[oi]))
                elif oi is None:
                    lines.append(f"*{news[nj]}*")
                    changes.append(Change(kind="new_provision",
                                          added=news[nj]))
                else:
                    marked, ch = _word_diff(olds[oi], news[nj])
                    lines.append(marked)
                    changes.extend(ch)
    return Redline(markdown="\n".join(lines), changes=changes)

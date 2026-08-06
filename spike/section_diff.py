"""V2 spike: statute-version redlines from archived chaptered bills.

Because amended sections are re-enacted at length (Cal. Const. art. IV,
Sec. 9), every chaptered bill lob carries the complete text of each
section as it read after that amendment. archive.db already stores the
flattened text of every bill version, so historical statute versions are
recoverable without new source data.

Questions this spike answers (scoping the v2 compare tools):
1. Can one code section's text be extracted reliably from a *flattened*
   chaptered-bill lob (multiple SEC. blocks, omnibus bills, dual
   operative-date variants of the same section)?
2. Does a word-level diff of two versions yield a faithful redline in
   markdown that chat UIs actually render (*italic* = added,
   ~~strikeout~~ = deleted — the official bill-print convention)?
3. What do the CAML insertion/deletion marks in amended-version XML
   encode, and against which baseline? (If usable, bill-to-bill
   redlines come straight from Legislative Counsel, no diffing.)

Usage (from repo root):
  python spike/section_diff.py diff  <pubinfo_zip> <bill_id> "<code name>" <section>
  python spike/section_diff.py marks <pubinfo_zip> <bill_id>
  python spike/section_diff.py chain <pubinfo_zip> "<code name>" <section> \
      <bill_id[:block]|@textfile>... <out_dir>

Examples:
  python spike/section_diff.py diff  pubinfo_2023.zip 202320240AB557 "Government Code" 54953
  python spike/section_diff.py marks pubinfo_2023.zip 202320240AB2302
"""

from __future__ import annotations

import difflib
import re
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ingest import caml, datfile  # noqa: E402
from ingest.tables import BILL_TABLES  # noqa: E402

VERSION_COLS = next(t for t in BILL_TABLES if t.name == "bill_version").columns


# --- version lookup --------------------------------------------------------

def load_versions(zf: zipfile.ZipFile, bill_id: str) -> list[dict]:
    rows = datfile.parse_bytes(zf.read("BILL_VERSION_TBL.dat"))
    out = [dict(zip(VERSION_COLS, r)) for r in rows]
    return sorted((v for v in out if v["bill_id"] == bill_id),
                  key=lambda v: int(v["version_num"] or 0))


# --- section block extraction from flattened bill text ---------------------

# First enacting section prints as "SECTION 1." (no dot after SECTION),
# the rest as "SEC. 2." — both forms must split and match. Flattened lobs
# do not reliably newline before headings ("...do enact as
# follows:SECTION 1.Section 54953..."), so a heading right after '.' or
# ':' splits too.
_SEC_SPLIT = re.compile(r"(?:\n|(?<=[.:]))(?=SEC\.\s*\d|SECTION\s+\d)")
_SEC_HEAD = re.compile(r"(?:SEC\.|SECTION)\s*[\d.]+")
_ACTION = re.compile(
    r"\b(?:is|are)\s+(?:further\s+)?"
    r"(amended(?:\s+and\s+renumbered)?[^:]{0,120}?to\s+read|"
    r"added[^:]{0,120}?to\s+read|repealed)\s*:?")
_LINEAGE = re.compile(r",\s*(as\s+(?:amended|added)\s+by[^,]{0,200}?),?\s+is")


def section_blocks(flat_text: str, code_name: str, section: str) -> list[dict]:
    """Every SEC. block of a flattened bill lob that operates on
    `section` of `code_name`, with its heading, action, and body text."""
    hits = []
    for block in _SEC_SPLIT.split(flat_text):
        head = block[:600]
        target = re.search(
            rf"Section\s+{re.escape(section)}(?!\.?\d)\s+(?:of|is\s+added\s+to)"
            rf"\s+the\s+{re.escape(code_name)}", head)
        if not target:
            continue
        action = _ACTION.search(head)
        if not action:
            continue
        heading = _SEC_HEAD.match(block)
        lineage = _LINEAGE.search(head)
        body = block[action.end():].lstrip("\n ")
        body = re.sub(rf"^{re.escape(section)}\.\s*", "", body)
        hits.append({
            "heading": heading.group(0) if heading else "?",
            "action": action.group(1),
            "lineage": lineage.group(1) if lineage else None,
            "intro": " ".join(block[:action.end()].split()),
            "body": body.strip(),
        })
    return hits


# --- word-level redline ----------------------------------------------------

_WORD = re.compile(r"\S+")
_SUBDIV = re.compile(r"^\([a-zA-Z0-9]{1,4}\)$")


def _tokens(text: str) -> list[str]:
    return _WORD.findall(text)


def redline(old: str, new: str) -> tuple[str, list[dict]]:
    """Markdown redline (*added* / ~~deleted~~) plus a structured change
    list. Whitespace-insensitive: both texts are treated as flowed words,
    so line-wrap differences between sources never produce phantom edits."""
    a, b = _tokens(old), _tokens(new)
    sm = difflib.SequenceMatcher(a=a, b=b, autojunk=False)
    out: list[str] = []
    changes: list[dict] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            out.extend(a[i1:i2])
            continue
        deleted, added = " ".join(a[i1:i2]), " ".join(b[j1:j2])
        if deleted:
            out.append(f"~~{deleted}~~")
        if added:
            out.append(f"*{added}*")
        changes.append({
            "context_before": " ".join(a[max(0, i1 - 6):i1][-6:]),
            "deleted": deleted or None,
            "added": added or None,
            "context_after": " ".join(a[i2:i2 + 6]),
        })
    # Re-break the flowed result before subdivision markers so it reads
    # like a statute again (display heuristic only; not part of the diff).
    lines: list[list[str]] = [[]]
    for tok in out:
        bare = tok.strip("*~")
        if (_SUBDIV.match(bare) and lines[-1]
                and lines[-1][-1].strip("*~")[-1:] in ".:;"):
            lines.append([])
        lines[-1].append(tok)
    return "\n".join(" ".join(ln) for ln in lines if ln), changes


# --- subdivision-anchored redline ------------------------------------------
# Flat word-level diff scrambles wholesale rewrites: SequenceMatcher
# anchors on incidental shared words across unrelated provisions. Legal
# redlines need structure: align subdivision-ish segments first, pair
# changed segments by similarity, word-diff only within pairs; unpaired
# segments strike out / insert whole.

_SEG_MARK = re.compile(r"(?<=[.:;])\s+(?=\([a-zA-Z0-9]{1,4}\)\s)")


def _segments(text: str) -> list[str]:
    """Split flowed statute text before subdivision markers that follow
    sentence-ending punctuation. Derived from content, not source line
    breaks, so both sources segment identically despite layout noise."""
    return _SEG_MARK.split(" ".join(text.split()))


def _merge_ops(ops: list) -> list:
    """Absorb equal runs of <=2 tokens sandwiched between changes: the
    matcher anchoring on an incidental 'of' splits one logical
    replacement into three unreadable hunks."""
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


def _word_diff(old: str, new: str) -> tuple[str, list[dict]]:
    a, b = _tokens(old), _tokens(new)
    sm = difflib.SequenceMatcher(a=a, b=b, autojunk=False)
    out, changes = [], []
    for tag, i1, i2, j1, j2 in _merge_ops(sm.get_opcodes()):
        if tag == "equal":
            out.extend(a[i1:i2])
            continue
        deleted, added = " ".join(a[i1:i2]), " ".join(b[j1:j2])
        if deleted:
            out.append(f"~~{deleted}~~")
        if added:
            out.append(f"*{added}*")
        changes.append({
            "context_before": " ".join(a[max(0, i1 - 6):i1][-6:]),
            "deleted": deleted or None,
            "added": added or None,
            "context_after": " ".join(a[i2:i2 + 6]),
        })
    return " ".join(out), changes


_PAIR_FLOOR = 0.5  # below this ratio, treat as delete + insert, not edit


def _pair(olds: list[str], news: list[str]) -> list[tuple[int | None,
                                                          int | None]]:
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


def redline_anchored(old: str, new: str) -> tuple[str, list[dict]]:
    A, B = _segments(old), _segments(new)
    sm = difflib.SequenceMatcher(a=A, b=B, autojunk=False)
    lines: list[str] = []
    changes: list[dict] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            lines.extend(A[i1:i2])
        elif tag == "delete":
            for s in A[i1:i2]:
                lines.append(f"~~{s}~~")
                changes.append({"kind": "deleted_provision", "text": s})
        elif tag == "insert":
            for s in B[j1:j2]:
                lines.append(f"*{s}*")
                changes.append({"kind": "new_provision", "text": s})
        else:
            olds, news = A[i1:i2], B[j1:j2]
            for oi, nj in _pair(olds, news):
                if nj is None:
                    lines.append(f"~~{olds[oi]}~~")
                    changes.append({"kind": "deleted_provision",
                                    "text": olds[oi]})
                elif oi is None:
                    lines.append(f"*{news[nj]}*")
                    changes.append({"kind": "new_provision",
                                    "text": news[nj]})
                else:
                    marked, ch = _word_diff(olds[oi], news[nj])
                    lines.append(marked)
                    for c in ch:
                        c["kind"] = "edit"
                    changes.extend(ch)
    return "\n".join(lines), changes


# --- CAML insertion/deletion mark survey -----------------------------------

_MARK = re.compile(r"<\?(xm-[a-z_]+)(.*?)\?>", re.DOTALL)


def survey_marks(xml: str, show: int = 4) -> None:
    counts: dict[str, int] = {}
    samples: dict[str, list[str]] = {}
    for m in _MARK.finditer(xml):
        name = m.group(1)
        counts[name] = counts.get(name, 0) + 1
        if len(samples.setdefault(name, [])) < show:
            start = max(0, m.start() - 200)
            ctx = xml[start:m.end() + 200]
            samples[name].append(re.sub(r"\s+", " ", ctx))
    for name, n in sorted(counts.items()):
        print(f"\n### {name}: {n} occurrences")
        for s in samples[name]:
            print(f"  …{s}…\n")


# --- entry points ----------------------------------------------------------

def cmd_diff(zip_path: str, bill_id: str, code_name: str,
             section: str) -> None:
    with zipfile.ZipFile(zip_path) as zf:
        versions = load_versions(zf, bill_id)
        chp = [v for v in versions if v["bill_version_id"].endswith("CHP")]
        if not chp:
            sys.exit(f"no chaptered version for {bill_id}")
        v = chp[0]
        print(f"# {bill_id} — {v['bill_version_id']} ({v['action_date']})")
        flat = caml.bill_text(
            zf.read(v["lob_file"]).decode("utf-8", errors="replace"))
        blocks = section_blocks(flat, code_name, section)
        print(f"{len(blocks)} block(s) operate on {code_name} § {section}\n")
        for b in blocks:
            print(f"## {b['heading']} — {b['action']}")
            print(f"   lineage: {b['lineage'] or '(none stated)'}")
            print(f"   intro:   {b['intro'][:160]}")
            print(f"   body:    {len(b['body'])} chars, "
                  f"starts: {b['body'][:80]!r}\n")


def cmd_chain(zip_path: str, code_name: str, section: str,
              specs: list[str], out_dir: str) -> None:
    """Redline consecutive pairs along a version chain.

    Each spec is `bill_id[:block_index]` (block_index defaults to 0, for
    bills whose chaptered lob carries several variants of the section —
    operative-date splits), or `@file.txt` for a plain-text endpoint
    (e.g. current law text fetched from the server).
    """
    endpoints: list[tuple[str, str]] = []  # (label, body)
    with zipfile.ZipFile(zip_path) as zf:
        for spec in specs:
            if spec.startswith("@"):
                endpoints.append(
                    (Path(spec[1:]).stem,
                     Path(spec[1:]).read_text().strip()))
                continue
            bill_id, _, idx = spec.partition(":")
            versions = load_versions(zf, bill_id)
            chp = [v for v in versions if v["bill_version_id"].endswith("CHP")]
            if not chp:
                sys.exit(f"no chaptered version for {bill_id}")
            flat = caml.bill_text(
                zf.read(chp[0]["lob_file"]).decode("utf-8", errors="replace"))
            blocks = section_blocks(flat, code_name, section)
            i = int(idx or 0)
            if i >= len(blocks):
                sys.exit(f"{bill_id}: only {len(blocks)} block(s), "
                         f"asked for index {i}")
            b = blocks[i]
            print(f"{spec}: using {b['heading']} "
                  f"(lineage: {b['lineage'] or 'none stated'})")
            endpoints.append((f"{bill_id}_{b['heading'].replace(' ', '')}"
                              .replace(".", ""), b["body"]))
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    for (la, ta), (lb, tb) in zip(endpoints, endpoints[1:]):
        marked, changes = redline_anchored(ta, tb)
        flat_marked, flat_changes = redline(ta, tb)
        path = out / f"redline_{la}__to__{lb}.md"
        report = [f"# {code_name} § {section}: {la} → {lb}",
                  f"\n{len(changes)} change(s)\n", "## Redline (anchored)\n",
                  marked, "\n## Change summary\n"]
        for c in changes:
            if c["kind"] == "edit":
                report.append(
                    f"- edit: …{c['context_before']} "
                    f"[{'-' + c['deleted'] + '-' if c['deleted'] else ''}"
                    f"{' ' if c['deleted'] and c['added'] else ''}"
                    f"{'+' + c['added'] + '+' if c['added'] else ''}] "
                    f"{c['context_after']}…")
            else:
                report.append(f"- {c['kind']}: {c['text'][:120]}…")
        report += [f"\n## Flat word diff, for comparison "
                   f"({len(flat_changes)} hunks)\n", flat_marked]
        path.write_text("\n".join(report))
        print(f"  wrote {path}  (anchored: {len(changes)}, "
              f"flat: {len(flat_changes)})")


def cmd_marks(zip_path: str, bill_id: str) -> None:
    with zipfile.ZipFile(zip_path) as zf:
        versions = load_versions(zf, bill_id)
        for v in versions:
            print(f"{v['bill_version_id']}  num={v['version_num']}  "
                  f"{v['action_date']}  {v['action']}  lob={v['lob_file']}")
        amended = [v for v in versions if "Amended" in (v["action"] or "")]
        if not amended:
            sys.exit("no amended version to survey")
        v = amended[-1]  # earliest amendment (version_num counts down)
        print(f"\n== marks in {v['bill_version_id']} ({v['action']}, "
              f"{v['action_date']}) ==")
        survey_marks(
            zf.read(v["lob_file"]).decode("utf-8", errors="replace"))


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "marks":
        cmd_marks(sys.argv[2], sys.argv[3])
    elif len(sys.argv) >= 2 and sys.argv[1] == "diff":
        cmd_diff(sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5])
    elif len(sys.argv) >= 2 and sys.argv[1] == "chain":
        cmd_chain(sys.argv[2], sys.argv[3], sys.argv[4],
                  sys.argv[5:-1], sys.argv[-1])
    else:
        sys.exit(__doc__)

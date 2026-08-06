# V2 Spike Findings — statute & bill version comparison

Spike code: `spike/section_diff.py`. Test corpus: `pubinfo_2023.zip`
(2023–2024 session, 1.26 GB) + current law text from the live server.
Test section: Gov. Code § 54953 (Brown Act) — three amending chapters in
one session (Stats. 2023 chs. 131, 534; Stats. 2024 ch. 389), sunset
branches, double-jointing. Deliberately the messiest realistic case.

## Verdict

Both v2 compare features are feasible on data we already have. Statute
redlining works from flattened chaptered-bill text exactly as stored in
archive.db — no schema change needed for archived sessions. Bill-version
redlining is even better than planned: the Legislature's own redline is
embedded in every amended-version lob.

## 1. Historical statute text — recoverable, with real-world hazards

Every chaptered bill re-enacts amended sections at length (Cal. Const.
art. IV, § 9), so § X "as of chapter Y" is extractable from the chaptered
lob. Findings from real extraction:

- **SEC-block splitting on flattened text works** after two fixes: the
  first enacting section prints `SECTION 1.` (no dot after SECTION),
  and flattened lobs do not reliably newline before headings
  (`...do enact as follows:SECTION 1.Section 54953...`), so headings
  directly after `.`/`:` must split too. Residual risk: a literal
  "SEC. n" inside quoted statutory text would mis-split. The robust
  long-term path is XML-side extraction at build time —
  `caml:BillSection` elements carry structured targeting
  (`<caml:ActionLine action="IS_AMENDED" xlink:href="urn:caml:codes:GOV:…">`),
  no regex needed — but that requires the source zips, not archive.db.
- **Sunset/operative-date branches are the norm, not the edge case.**
  AB 2449 (2022) left *three* parallel versions of § 54953; each 2023
  bill carries 3–4 blocks for the same section. The intro sentence's
  lineage parenthetical ("as amended by Section 2 of Chapter 285 of the
  Statutes of 2022") is machine-extractable and is how a compare tool
  must order the version graph. A compare tool should return all
  matching blocks with lineage and let the caller pick, defaulting to
  the chain named in the section's history note.
- **Double-jointing fuzz**: AB 557's operative block prints as
  `SEC. 1.5.` but later bills cite it as "Section 1 of Chapter 534" —
  lineage matching needs tolerance.
- **No phantom diffs across sources.** Chaptered-lob text vs current-law
  lob text diff clean: quote conventions, whitespace, and subdivision
  layout all normalize away with whitespace-insensitive word
  tokenization. The final AB 557 → current redline contains only real
  SB 707 changes.
- Pre-1999 archive sessions carry chaptered text only — sufficient for
  statute redlining (chaptered is all it needs).

## 2. Diff engine — subdivision-anchored, not flat

Flat word-level `difflib.SequenceMatcher` is **not acceptable** for
legal text: on wholesale rewrites it anchors on incidental shared words
and interleaves unrelated provisions (produced hunks like
`~~meeting during~~ *physical disability or*`). The working recipe,
all stdlib, interactive-speed:

1. Segment both versions before subdivision markers (`(a)`, `(1)`,
   `(A)`, `(i)`) that follow sentence-ending punctuation — content-derived,
   so layout noise between sources doesn't matter.
2. Align segments with SequenceMatcher; pair segments inside `replace`
   ranges by similarity ratio (floor 0.5, order-preserving greedy).
   Unpaired segments render as whole-provision strikeout/insert.
3. Word-diff only within paired segments; absorb equal runs of ≤2
   tokens between adjacent changes (kills the "anchored on a stray
   'of'" hunk-splitting).

Output quality on the hard case (SB 707's rewrite): relettering renders
as `~~(c)~~ *(d)*`, repealed COVID-era subdivisions strike out whole,
and compound edits read exactly as an attorney would write them:
`…fringe benefits of ~~a local agency executive, as defined in
subdivision (d) of Section 3511.1,~~ *either of the following*…`.

**Precomputing diffs: rejected.** Extraction + diff is interactive-speed;
pairs are quadratic in versions; nothing to gain.

## 3. Display format for LLM output

- Convention: ***italics* = added, ~~strikeout~~ = deleted** — mirrors
  official California bill printing, so attorneys read it natively, and
  both are faithfully renderable markdown in Claude and ChatGPT chat UIs.
- Underline is unachievable (no markdown syntax; chat UIs sanitize
  `<u>`). Code blocks are wrong for this: nothing renders inside them,
  so `~~…~~` shows as literal tildes. (` ```diff ` blocks color whole
  lines only — too coarse for statute paragraphs.)
- The server emits final display-ready markdown plus a structured change
  summary (edit / new_provision / deleted_provision entries with
  context), and an envelope note instructing the model to reproduce the
  redline verbatim, not re-derive it. Models are unreliable at
  *computing* diffs but reliable at *copying* text.

## 4. Bill versions — the official redline is already in the data

- Archive stores the full text of **every printed version** (introduced,
  each amended, enrolled, chaptered) for 1999+; pre-1999 chaptered only.
  current.db stores only titles — v2 must keep current-session version
  text (same extraction path as the archive builder).
- Amended-version XML embeds Legislative Counsel's own redline:
  `<?xm-deletion_mark data=" deleted text"?>` (deleted text lives in the
  PI's data attribute, possibly with escaped markup for whole deleted
  paragraphs) and `<?xm-insertion_mark_start?>…<?xm-insertion_mark_end?>`
  around insertions. Marks appear in both digest and body (40–52 body
  marks in AB 557's amended prints) and redline against the
  **immediately preceding printed version** of the bill (verified via
  digest wording changes across AB 557's prints).
- So consecutive-version bill redlines should come from the marks
  (authoritative, matches the official print); computed diffs remain the
  fallback for non-adjacent pairs (e.g. introduced vs chaptered) and for
  version pairs where marks are unavailable.
- v1's `caml.bill_text` deliberately strips marks; a v2 parser variant
  converts them to redline output instead. The deletion PI's escaped-
  markup payloads need their own flattening pass.
- Analyses already carry `amendment_date`, so each version-to-version
  redline can be paired with the analyses written on that print — the
  "when did this phrase enter, and what did the committee say"
  workflow.

## Open items for SPEC scoping

1. Tool surface: `compare_section_versions`, `compare_bill_versions`,
   `get_bill_text` (bill text is currently stored but unserved).
2. Where historical section extraction runs: on-demand from archive.db
   flattened text (works today, regex-based) vs precomputed at build
   time from XML (structurally robust, needs source zips at build).
3. Storing current-session bill version text in current.db (size cost:
   one session, zlib — measure in Phase 1 of v2).
4. Version-graph resolution: exposing lineage so the LLM can name
   versions the way attorneys do ("as amended by Stats. 2023, Ch. 534,
   Sec. 2").

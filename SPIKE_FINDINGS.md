# Spike Findings — CA Statutes & Legislation MCP Server

*Spike run 2026-08-05 against live pubinfo data from
downloads.leginfo.legislature.ca.gov. Code in `spike/`; database and raw
archives in the session scratchpad (not committed).*

## Verdict

All three features are proven end-to-end against real data. No open
feasibility questions remain. The riskiest component (bill title → sections
parser) achieved 100% classification on the full 2025–26 session with zero
unparsed residue.

## What was demonstrated

1. **`get_section`** — EDC 44955 returns statute text, history note
   ("Amended by Stats. 1983, Ch. 1302..."), effective date, hierarchy.
2. **`bills_affecting_section`** — PEN 1050 correctly returns AB 2052 and
   AB 1656, both at Third Reading, from the parsed-title table.
3. **Legislative history two-hop** — BPC 17539.1 → history note cites
   Stats. 2025, Ch. 623 → `bill` table maps chapter to AB 831 → 11
   committee/floor analyses in date order (chapter→bill is a **column
   join**, no parsing — `BILL_TBL` carries `CHAPTER_YEAR`/`CHAPTER_NUM`).

## Key numbers

| Metric | Value |
|---|---|
| Full corpus build (2025–26 session + all current law) | **22 seconds** |
| DB size: 162,414 statutes + session bills, no FTS | 340 MB |
| DB size with FTS5 over all statute text | 411 MB |
| Title parse, 16,247 versions | 84.7% parsed, 15.3% correctly classified (sectionless/budget/uncodified), **0 failures** |
| Section refs extracted | 43,081 |
| Amend/repeal refs resolving to live sections | 97.95% (misses = repealed/not-yet-added sections — real, useful signal) |
| Analysis extraction (30 sampled per era × 3 formats) | 90/90 clean |
| Legacy .doc conversion throughput (LibreOffice batch) | 30 files / 2.4 s |

## Era coverage matrix (verified per-session, not assumed)

| Sessions | Bills | Analyses | History/votes | Analysis format |
|---|---|---|---|---|
| 1989–1991 | chaptered only, final version | — | — | — |
| 1993 | chaptered only | ✓ (26k) | — | plain text |
| 1995–1997 | chaptered only | ✓ | — | HTML |
| 1999–2005 | **all bills, all versions** | ✓ | ✓ (1999+; hearings 2005+) | HTML |
| 2009–2013 | all | ✓ | ✓ | binary .doc |
| 2015/2017–present | all | ✓ | ✓ | .docx |
| 2013–present | all | ✓ | ✓ | + law-table snapshots in archive |

Implications for the pitch to attorneys:
- Committee analyses reach back to **1993**, not 1989.
- Pre-1999: **chaptered bills only** — fine for statute history (you only
  chase enacted bills), but amendment-evolution comparison starts 1999.
- Bill text is CAML XML in every era back to 1989 — one parser.
- Sections last amended before 1993 → "predates electronic records"
  response (EDC 44955, amended 1983, is the canonical example).
- No PDFs anywhere. No OCR. Four text formats, dispatched on file magic
  (plain text / HTML / .doc / .docx). Exact transition years don't matter
  because era is never trusted — 2015 may well be mixed .doc/.docx.

## Traps discovered (design requirements)

1. **704 sections exist in multiple simultaneous versions** (future
   operative dates, competing chapters, inoperative windows).
   `get_section` must return *all* versions with effective dates — a
   single-row lookup would mislead.
2. **Section numbers carry trailing periods** in `LAW_SECTION_TBL`
   ("44955.") — normalize on load, index the normalized form.
3. **Statute text is weekly-fresh** (Sunday full dump); bills/analyses are
   daily. Every response should carry both extract timestamps. Law tables
   are TRUNCATE+reload artifacts; bill tables are REPLACE upserts. If a
   daily incremental ever ships law tables (likely January), reload them
   from it — semantics safe either way.
4. **Leg Counsel titles contain real typos** (missing "Section", missing
   "to", "Welfare and Institution Code", lowercase "section", arabic
   article numbers). Parser now tolerates all observed forms; expect more
   in archives.
5. **Title categories that are not code amendments**: Budget Act
   amendments (sections of a session law), uncodified district acts,
   constitutional amendments via ACA/SCA ("Article IV thereof"),
   appropriations boilerplate ("Section 12 of Article IV"). Each needs its
   own classification, not a parse failure.
6. **~2% of amend refs target sections not in current law** — bills
   amending repealed sections or sections another pending bill would add.
   Surface as a flag, don't drop.
7. **docx field codes** (MERGEFIELD) leak into naive XML text extraction —
   strip `<w:instrText>` runs.
8. Statute XML can contain tables; current flattening loses structure —
   acceptable for verification text, note in tool docs.

## Corpus size projection

Current-session DB is 411 MB with statute FTS. Archives: ~420k analyses
(≈26k/session × 16 sessions with analyses) at ~5–10 KB extracted text ≈
3–4 GB plus chaptered version text pre-1999 and full version text 1999+.
Two-artifact design confirmed: hot DB (current law + session, rebuilt
nightly, ~400 MB) + cold archive DB (built once, single-digit GB).
R2 storage cost at this scale is cents per month.

## One-time archive build cost

~8.5 GB download (all sessions), extraction dominated by .doc conversion:
~400k docs at LibreOffice batch rates ≈ a few hours single-threaded,
parallelizable. Entirely tractable as a one-shot local job.

## Spike code map

- `spike/datfile.py` — .dat parser (MySQL LOAD DATA conventions)
- `spike/caml.py` — CAML XML → text (statutes, bill titles)
- `spike/build_db.py` — session zip → SQLite (22 s)
- `spike/titles.py` — title → (action, code, sections) parser
- `spike/coverage.py` — parser coverage report
- `spike/analyses.py` — analysis format dispatch + extraction
- `spike/era_survey.py` — per-session archive inventory
- `spike/demo.py` — the three features end-to-end

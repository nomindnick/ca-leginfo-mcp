# SPEC — California Statutes & Legislation MCP Server

*Authoritative build specification. Verified data-source facts and spike
results live in [SPIKE_FINDINGS.md](SPIKE_FINDINGS.md); this document
defines what to build. Status: approved for full build.*

## 1. Purpose

An MCP server exposing California codified law and legislative data so an
attorney's AI tool of choice can:

1. **Verify statutory text from source** — leginfo.ca.gov blocks AI
   crawlers via robots.txt; the underlying data is publicly published for
   bulk download. This server is the bridge.
2. **Flag pending bills** that would amend/repeal/add a given code section.
3. **Retrieve legislative history** — from a code section to its enacting
   or amending bills and their committee/floor analyses, back to 1993.

Primary users: a small pilot group of California public-agency attorneys.
Positioning: between a demo and a daily-use tool; if adopted, a firm-funded
deployment follows.

## 2. Scope (agreed)

**In (v1):**
- Current law: all 29 codes + Constitution (162k+ sections), full text, FTS.
- Current session: all bills, versions, status, history, analyses, votes,
  vetoes; parsed bill→section reference table.
- Full archives 1989→present: chapter→bill mapping, committee/floor
  analyses (text-extracted), bill version text, history/votes where the
  era provides them. Join-only access (no FTS over archive analyses in v1).
- Nightly corpus refresh; two extract timestamps on every response.

**Out (v1, candidates for later):**
- FTS over archive analyses (v1.1 — additive index rebuild, no schema change).
- Point-in-time historical statute text (2013+ archives include law
  snapshots; future feature).
- Embeddings/semantic search. Webapp. Multi-state.

## 3. Architecture

```
                 ┌──────────────────────────────────────────┐
 downloads.      │ Ingest (Python pkg, grown from spike/)   │
 leginfo.        │  builder: session zip(s) → SQLite        │
 legislature.    │  • current.db  nightly, ~420 MB          │
 ca.gov  ───────▶│  • archive.db  one-time, single-digit GB │
                 └───────────────┬──────────────────────────┘
                                 │ upload (atomic: tmp name + rename)
                          Cloudflare R2
                                 │ download on boot / daily check
                 ┌───────────────▼──────────────────────────┐
                 │ MCP server (FastMCP, Railway)            │
                 │ streamable HTTP; read-only SQLite        │
                 └──────────────────────────────────────────┘
```

- **Two artifacts.** `current.db` = current law + current session, rebuilt
  nightly. `archive.db` = all prior sessions, built once, rebuilt only on
  pipeline fixes or biennial session rollover.
- **Nightly job (stateless, drift-free):** download `pubinfo_daily_[Day].zip`
  (all current-session bill data, ~950 MB) + reuse cached Sunday
  `pubinfo_YYYY.zip` for law tables (re-fetch Sundays) + check the small
  `pubinfo_[Day].zip` incremental — if it ever contains `LAW_*` tables
  (expected around January), apply them over the cached law tables. Build
  fresh DB, sanity-check (row counts ≥ previous, spot-check known
  sections), upload to R2, server picks up. Never merge into an existing
  DB — full rebuild every night (build is ~seconds-to-minutes).
- **Runner:** Railway cron service (same repo, ingest entrypoint).
  Fallback if Railway disk/bandwidth is awkward: GitHub Actions nightly.
- **Server:** follows the user's existing FPPC-project deployment pattern
  (Railway + R2). Read-only public legal data; simple rate limiting.

## 4. Database schema

`current.db` (from spike, kept; additions marked *): `codes`, `law_section`
(+`content_text`, `section_num_norm`), `law_toc`, `law_toc_sections`,
`bill`, `bill_version` (+`title_text`), `bill_version_authors`\* (tool 4
returns authors), `bill_history`, `bill_analysis`, `analysis_text`\*
(zlib-compressed extracted text keyed by analysis_id — tool 5 must return
text for current-session bills; the 2025–26 session is 100% .docx, so the
nightly build needs no LibreOffice), `veto_message`, `bill_section_ref`
(parsed titles: bill_version_id, bill_id, action, law_code, section,
is_range, range_end, struct, `exists_in_current_law`\* — precomputed §6
flag), `law_fts` (FTS5 over statute text), `meta` (extract dates, build
time, session, source zips, title-parse coverage). Vote tables stay
archive-only for now (§2's "votes" is served from archives; add to
current.db when a tool needs them — nightly rebuilds make schema changes
free).

`archive.db` (as built, 1989–2023, 4.7 GB): `bill`, `bill_version`,
`bill_version_authors`, `bill_version_text` (title + zlib-compressed full
text per version — zlib chosen over zstd for stdlib-only builds),
`bill_history`, `bill_analysis` + `analysis_text` (extracted text keyed by
analysis_id, zlib), `bill_detail_vote`/`bill_summary_vote`/`bill_motion`
(motion text makes votes interpretable), `veto_message` + `veto_text`
(lobs exist 2011+), `bill_section_ref` (parsed from archived titles, +
session_year), `session_coverage` (per-session matrix: row counts, title
coverage, analysis formats/errors — so tools can state limits honestly),
`meta`.

Indexes: `(law_code, section_num_norm)`; `(chapter_year, chapter_num)`;
`bill_id` on bill/history/analyses/refs; `(law_code, section)` on refs.
Both artifacts ship in rollback-journal mode (the archive's WAL is
checkpointed and reset at end of build — WAL can't be read from a
read-only filesystem).

## 5. MCP tool surface

*As built (`server/`, Phase 3): the SDK is `mcp` 2.0 (`MCPServer` — the
API formerly named FastMCP); tool logic lives in `server/tools.py` as
plain functions so tests run without a client. Deltas from the plan are
marked below.*

Every response includes: `law_extract_date`, `bill_extract_date`,
`current_session`, optional `notes` (coverage/limit statements), and a
`source` note (data derived from the Legislature's public bulk downloads;
not the official publication).

1. `get_section(code, section)` — **all** versions of the section (704
   sections have multiple simultaneous versions — future operative dates,
   competing chapters), each with text, history note, effective date,
   hierarchy (division/part/chapter/article headings via `law_toc`).
   Normalize input (trailing periods, "Gov. Code" → GOV aliases).
2. `search_sections(query, code?, limit?)` — FTS5 over current statute
   text; returns code, section, heading, snippet.
3. `bills_affecting_section(code, section)` — pending bills whose latest
   version's parsed title references the section (direct hit, range
   containment, or structural add commencing at/near it), with action
   (amend/repeal/add), bill status, location, latest analysis date.
4. `get_bill(measure, session?)` — accepts "AB 831" style or bill_id;
   returns status, chapter (if enacted), authors, history actions,
   version list, parsed sections affected.
5. `get_bill_analyses(measure?, session?, analysis_id?)` — analysis index
   (committee, house, date) + full extracted text on request
   (`analysis_id` fetch). Works across current + archive.
6. `get_legislative_history(code, section)` — the flagship: parse the
   section's history note citation(s), resolve each chapter → bill via
   archive/current `bill` tables, return per-chapter: bill, authors,
   analyses index, veto messages. As built, events carry a `role`
   (operative vs the "(as amended by …)" prior-version parenthetical)
   and three extra branches: voter initiatives ("by initiative
   Proposition 47" and the Constitution's "by Prop. N. Initiative
   measure." form) return an adopted-by-initiative marker; Constitution
   "Res.Ch. N, YYYY" citations resolve via resolution chapters to the
   proposing SCA/ACA; extraordinary-session citations resolve via
   `chapter_session_num`. Pre-1989 citations return an explicit marker
   (bill data begins 1989, analyses 1993 — more precise than the
   planned flat 1993 cutoff). A supplementary
   `enacted_bills_citing_section` list reconstructs the earlier lineage
   from archived bill titles (1989→present, title-based).
7. `chapter_to_bill(year, chapter, kind?, ex_session?)` — direct pivot,
   any session ≥1989; `kind="resolution"` for Res.Ch. citations. The
   chapter key is not unique in real pubinfo data (adjacent sessions'
   organizing resolutions share "Res. Ch. 1"; a few duplicate chapter
   records exist) — multiple matches return the first deterministically
   plus a warning naming the others. `get_bill` additionally returns
   floor/committee vote summaries (with motion text) for archive bills.

Error behavior: never empty-and-silent. Unknown section → nearest-match
suggestions (same code, prefix match). Archive gaps → explicit coverage
statement from `meta` (e.g., "1995–96 archive contains chaptered bills
only").

## 6. Data-handling rules (from spike traps)

- Dispatch analysis extraction on **file magic**, never era: plain text /
  HTML (bs4) / legacy .doc (LibreOffice headless, batched) / .docx
  (stdlib zip + document.xml, strip `<w:instrText>` field codes).
- Title parser categories: `ok`, `no_sections`, `budget_act`,
  `uncodified`, plus typo tolerance (missing "Section"/"to", lowercase
  "section", "Welfare and Institution Code", arabic article numbers).
  Parse failures in archives: log, count, never crash the build; keep
  coverage ≥99% per session or investigate.
- Refs to sections absent from current law are kept and flagged
  (`exists_in_current_law: false`) — they signal repealed targets or
  cross-bill contingencies.
- Law tables are TRUNCATE+reload artifacts; bill tables REPLACE upserts.
  Never partial-merge law tables.
- `.dat` parsing per MySQL LOAD DATA conventions (tab-delimited, backtick
  enclosure, backslash escapes, bare NULL) — spike `datfile.py` is correct.
- CONS sections are article-scoped ("Art. I, Sec. 3") — normalize
  consistently between law tables and parsed constitutional refs.

## 7. Build phases

1. **Ingest package** — productionize `spike/` into `ingest/` (typed,
   tested; unit tests on real .dat/.lob fixtures), `current.db` builder
   CLI, sanity-check gate.
2. **Archive builder** — download all sessions (~8.5 GB), format-dispatch
   analysis text extraction (~420k docs; hours, parallelize LibreOffice),
   `archive.db` builder, per-session coverage `meta`.
3. **MCP server** — FastMCP app, 7 tools, response envelope, tests against
   spike-built DBs; local stdio testing then HTTP.
4. **Deploy** — R2 buckets, Railway server + cron, boot-time DB sync,
   smoke tests via Claude; pilot rollout to interested attorneys.

Each phase ends with a working artifact the user can inspect. Sprint
compliance is reviewed against this SPEC (see `/plan-review`).

## 8. Non-goals & honesty constraints

This is an unofficial convenience mirror of public data. Responses must
carry extract dates, note the weekly statute freshness reality, and direct
users to the official publication for filings. It never gives legal
advice; it retrieves law.

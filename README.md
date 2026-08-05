# ca-leginfo-mcp

An MCP server for California statutes and legislation, built on the
Legislature's official public bulk-data channel
(downloads.leginfo.legislature.ca.gov).

Lets an attorney's AI tool:

- **Verify statutory text from source** — current text, history note, and
  effective date for any section of the 29 California codes or the
  Constitution
- **Flag pending bills** that would amend, repeal, or add a given section
- **Retrieve legislative history** — from a code section to its enacting
  and amending bills and their committee/floor analyses, back to 1993

## Status

Phases 1–2 (ingest pipeline + archives) complete. The
[`ingest/`](ingest/) package builds both corpus artifacts from the
pubinfo bulk zips:

- `current.db` (~40 s nightly): current law + 2025–26 session, FTS,
  parsed bill→section refs, analysis text — with a sanity gate that
  blocks bad artifacts (`python -m ingest build` / `sanity`).
- `archive.db` (4.7 GB, 18 sessions 1989–2023, ~35 min once):
  283k bill version texts, 371k text-extracted committee/floor analyses,
  votes, vetoes, ~1M parsed section refs, per-session coverage matrix
  (`python -m ingest build-archive`).

Next per [SPEC.md](SPEC.md): MCP server (Phase 3), deployment (Phase 4).
The feasibility spike that preceded the build is preserved in
[`spike/`](spike/) — see [SPIKE_FINDINGS.md](SPIKE_FINDINGS.md).

## Data source

All data derives from the California Legislature's public "pubinfo" bulk
downloads — the sanctioned distribution channel for this data (the
`capublic` schema published at downloads.leginfo.legislature.ca.gov).
No scraping is involved.

**This is an unofficial convenience mirror.** Statute text refreshes
weekly (bills daily); every response carries its extract dates. For court
filings, verify against the official publication.

## Disclaimer

This tool retrieves law; it does not give legal advice.

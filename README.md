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

All four build phases complete (ingest pipeline, archives, MCP server,
deployment). The server runs on Railway with artifacts in Cloudflare R2,
rebuilt nightly ([`deploy/`](deploy/)):

    https://server-production-602d.up.railway.app/mcp   (streamable HTTP)

- [`ingest/`](ingest/) builds both corpus artifacts from the pubinfo
  bulk zips: `current.db` (~40 s nightly: current law + session, FTS,
  parsed bill→section refs, analysis text, sanity gate) and
  `archive.db` (4.7 GB, 18 sessions 1989–2023: 283k bill version texts,
  371k text-extracted analyses, votes, vetoes, ~1M parsed section refs,
  per-session coverage matrix).
- [`server/`](server/) serves both over MCP (`mcp` 2.0): seven tools —
  section text/search, pending-bill flagging, bill lookup, analyses
  with full text, legislative history (initiative and
  constitutional-amendment aware), chapter→bill pivot. Every response
  carries extract dates and a source note; era gaps come back as
  explicit coverage statements. Run locally:
  `ca-leginfo-server --current-db … --archive-db …` (stdio; `--transport
  http` for streamable HTTP).
- [`deploy/`](deploy/) is the Railway + R2 layer: `boot.py` syncs the
  artifacts from R2 and serves (`/health` reports freshness); a nightly
  cron (`nightly.py`, 10:00 UTC) rebuilds current.db from the freshest
  pubinfo daily zip, gates it against last night's artifact, and
  uploads only on PASS. Simple per-client rate limiting on HTTP.

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

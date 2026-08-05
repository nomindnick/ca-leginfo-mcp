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

Feasibility spike complete and successful — see
[SPIKE_FINDINGS.md](SPIKE_FINDINGS.md). Full build in progress per
[SPEC.md](SPEC.md). Spike prototype code is in [`spike/`](spike/).

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

"""Deployment layer (SPEC §3/§7 Phase 4): R2 artifact sync, server boot,
nightly rebuild. Railway runs two services from this repo's Dockerfile —
the MCP server (`python -m deploy.boot`) and the nightly ingest cron
(`python -m deploy.nightly`)."""

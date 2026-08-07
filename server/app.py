"""FastMCP wiring for the ten tools (SPEC §5, §12).

Tool logic lives in server/tools.py and server/texttools.py as plain
functions; this module only declares the MCP surface (names, parameter
schemas, descriptions the calling AI reads) and the transports: stdio
for local use, streamable HTTP for the Railway deployment.

Database paths come from CA_LEGINFO_CURRENT_DB / CA_LEGINFO_ARCHIVE_DB
(defaults: ./current.db, ./archive.db; a missing archive degrades the
history tools with explicit coverage notes rather than failing).
"""

from __future__ import annotations

import argparse
import logging
import os
from typing import Any

from mcp.server.mcpserver import MCPServer

from server import __version__, texttools, tools
from server.db import ARCHIVE_DB_ENV, CURRENT_DB_ENV, Databases

log = logging.getLogger("server.app")

_dbs: Databases | None = None


def _get_dbs() -> Databases:
    global _dbs
    if _dbs is None:
        _dbs = Databases.from_env()
    return _dbs


def configure(dbs: Databases) -> None:
    """Point the MCP tool layer at explicit databases (used by tests)."""
    global _dbs
    _dbs = dbs


mcp = MCPServer(
    name="ca-leginfo",
    version=__version__,
    instructions=(
        "California statutes and legislation, from the Legislature's "
        "official public bulk-data channel. Verify current statutory "
        "text (get_section, search_sections), find pending bills that "
        "would change a section (bills_affecting_section), and retrieve "
        "legislative history back to 1989 — enacting/amending bills, "
        "committee and floor analyses, veto messages, and floor/committee "
        "vote summaries on archived bills via get_bill "
        "(get_legislative_history, get_bill, get_bill_analyses, "
        "chapter_to_bill). Read any bill print's full text "
        "(get_bill_text) and compute display-ready redlines between "
        "versions of a code section — historical, current, or as a "
        "pending bill proposes (compare_section_versions) — or between "
        "two prints of one bill (compare_bill_versions). Every response "
        "carries law/bill extract dates and a source note; this is an "
        "unofficial mirror — direct users to the official publication "
        "for court filings."
    ),
)


@mcp.tool(structured_output=True)
def get_section(code: str, section: str) -> dict[str, Any]:
    """Current text of a California code section, with history note,
    effective date, and placement in the code's hierarchy.

    Returns ALL simultaneous versions when more than one exists (future
    operative dates or competing chapters). `code` accepts standard
    citation forms ("Gov. Code", "GOV", "Government Code", "CCP",
    "Cal. Const."). For the Constitution, `section` must be
    article-scoped, e.g. "Art. XIII B, Sec. 1".
    """
    return tools.get_section(_get_dbs(), code, section)


@mcp.tool(structured_output=True)
def search_sections(query: str, code: str | None = None,
                    limit: int = 10) -> dict[str, Any]:
    """Full-text search over current California statute text (FTS5).

    Returns code, section, nearest heading, and a match snippet per hit.
    `query` supports FTS5 syntax (quoted phrases, AND/OR/NOT/NEAR);
    plain words work fine. Optional `code` restricts to one code
    ("GOV", "Pen. Code", ...). `limit` caps results (max 50).
    """
    return tools.search_sections(_get_dbs(), query, code, limit)


@mcp.tool(structured_output=True)
def bills_affecting_section(code: str, section: str) -> dict[str, Any]:
    """Bills in the current legislative session whose latest version
    would amend, repeal, or add the given code section.

    Each bill carries status, location, chapter (if already enacted),
    latest analysis date, and how it matched: a direct citation, a
    containing range, a whole constitutional article, or a structural
    add commencing just below the section. Pending bills sort first.
    """
    return tools.bills_affecting_section(_get_dbs(), code, section)


@mcp.tool(structured_output=True)
def get_bill(measure: str, session: str | None = None) -> dict[str, Any]:
    """Status and contents of one bill: authors, complete history
    actions, version list, and the code sections its latest version
    affects.

    `measure` accepts "AB 831" style or a full bill_id
    ("202520260AB13"). `session` like "2023-2024" (default: current
    session); any session back to 1989-90 resolves from the archive.
    """
    return tools.get_bill(_get_dbs(), measure, session)


@mcp.tool(structured_output=True)
def get_bill_analyses(measure: str | None = None,
                      session: str | None = None,
                      analysis_id: str | int | None = None) -> dict[str, Any]:
    """Committee and floor analyses for a bill — the index (committee,
    house, date), or one analysis's full extracted text.

    Call with `measure` (+ optional `session`, back to 1993-94) for the
    index; call with `analysis_id` from a previous response for the full
    text. Works across the current session and the archive.
    """
    return tools.get_bill_analyses(_get_dbs(), measure, session,
                                   analysis_id)


@mcp.tool(structured_output=True)
def get_legislative_history(code: str, section: str) -> dict[str, Any]:
    """Legislative history of a code section: the bills behind the
    Stats./Res.Ch. citations in its history note, each with authors,
    an index of committee/floor analyses, and any veto messages.

    Also returns every enacted bill since 1989 whose final title cited
    the section (title-based lineage). Handles voter initiatives
    ("Proposition 47") and constitutional amendments (resolution
    chapters -> the proposing SCA/ACA) explicitly; citations before
    1989 are flagged as predating electronic records.
    """
    return tools.get_legislative_history(_get_dbs(), code, section)


@mcp.tool(structured_output=True)
def chapter_to_bill(year: int, chapter: int, kind: str = "statutes",
                    ex_session: int = 0) -> dict[str, Any]:
    """Resolve a session-law chapter citation to its bill, any year
    since 1989.

    "Stats. 2004, Ch. 183" -> year=2004, chapter=183. Use
    kind="resolution" for resolution chapters ("Res. Ch. 97, 2022",
    how constitutional amendments are chaptered) and ex_session for
    extraordinary-session citations ("Stats. 2009, 3rd Ex. Sess.,
    Ch. 17" -> ex_session=3).
    """
    return tools.chapter_to_bill(_get_dbs(), year, chapter, kind,
                                 ex_session)


@mcp.tool(structured_output=True)
def get_bill_text(measure: str, session: str | None = None,
                  version: str | None = None,
                  section_filter: str | None = None) -> dict[str, Any]:
    """Full flattened text of one bill print (title + digest + body),
    default the latest version.

    Oversized prints (>50k chars — omnibus/budget bills) return an index
    of enacting sections instead, each intro line naming the code section
    it operates on; re-query with `section_filter` ("GOV 54953") for
    those blocks' full text, including sunset/operative-date variants
    with their lineage. `version` accepts a version number, an action
    phrase ("introduced", "chaptered", "amended assembly"), a date, or a
    bill_version_id. Works across current session and archive (1989+;
    pre-1999 sessions store chaptered prints only).
    """
    return texttools.get_bill_text(_get_dbs(), measure, session, version,
                                   section_filter)


@mcp.tool(structured_output=True)
def compare_section_versions(code: str, section: str,
                             from_ref: str | None = None,
                             to_ref: str | None = None) -> dict[str, Any]:
    """Redline between two versions of a California code section —
    display-ready markdown (*italics* = added, ~~strikeout~~ = deleted)
    plus a structured change list. Reproduce the markdown verbatim.

    Refs accept a chapter citation ("Stats. 2023, Ch. 534"), "current",
    or a measure ("AB 405") for a pending bill's proposed text. Defaults:
    prior operative version → current law (the zero-argument redline);
    with only to_ref a measure, current law → its proposed text. Sunset
    branches resolve via the section's history-note chain; parallel
    variants are always listed, never silently picked. Identical
    endpoints return an affirmative no-change statement.
    """
    return texttools.compare_section_versions(_get_dbs(), code, section,
                                              from_ref, to_ref)


@mcp.tool(structured_output=True)
def compare_bill_versions(measure: str, session: str | None = None,
                          from_version: str | None = None,
                          to_version: str | None = None) -> dict[str, Any]:
    """Redline between two prints of one bill — what changed as it moved
    through the process. Reproduce the markdown verbatim.

    Defaults to the latest print vs. its predecessor; any pair works
    (introduced vs. chaptered answers "what changed overall"). Title +
    digest edits come first as their own part — Legislative Counsel's
    own summary of the change — then the body redline. Version args as
    in get_bill_text. Pair with get_bill_analyses' amendment_date for
    "when did this phrase enter and what did the committee say".
    Pre-1999 archive sessions are chaptered-only, so no version pairs
    exist there.
    """
    return texttools.compare_bill_versions(_get_dbs(), measure, session,
                                           from_version, to_version)


@mcp.custom_route("/health", methods=["GET"])
async def health(request):
    """Liveness + freshness for platform checks and monitoring."""
    from starlette.responses import JSONResponse

    dbs = _get_dbs()
    try:
        with dbs.current() as con:
            meta = dict(con.execute(
                "SELECT key, value FROM meta WHERE key IN "
                "('law_extract_date', 'bill_extract_date', 'build_utc')"))
    except Exception as e:  # noqa: BLE001 — health must answer, not raise
        return JSONResponse({"ok": False, "error": repr(e)}, status_code=503)
    return JSONResponse({"ok": True, "archive": dbs.has_archive, **meta})


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="ca-leginfo-server",
        description="MCP server for California statutes and legislation.")
    parser.add_argument("--transport", choices=("stdio", "http"),
                        default="stdio")
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"),
                        help="HTTP bind address (Railway needs HOST=0.0.0.0)")
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("PORT", "8000")))
    parser.add_argument("--current-db", help=f"overrides ${CURRENT_DB_ENV}")
    parser.add_argument("--archive-db", help=f"overrides ${ARCHIVE_DB_ENV}")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s")

    if args.current_db:
        os.environ[CURRENT_DB_ENV] = args.current_db
    if args.archive_db:
        os.environ[ARCHIVE_DB_ENV] = args.archive_db

    dbs = Databases.from_env()
    if not dbs.current_path.exists():
        parser.error(
            f"current.db not found at {dbs.current_path} "
            f"(set ${CURRENT_DB_ENV} or --current-db)")
    if not dbs.has_archive:
        log.warning("archive.db not found — history tools will be limited "
                    "to the current session")
    configure(dbs)
    log.info("current.db: %s; archive.db: %s", dbs.current_path,
             dbs.archive_path or "(none)")

    if args.transport == "stdio":
        mcp.run(transport="stdio")
        return

    # HTTP: build the Starlette app ourselves so we can add rate limiting
    # (SPEC §3). Behind Railway's proxy the Host header is the public
    # domain — enable DNS-rebinding protection only when ALLOWED_HOSTS
    # names it; local direct binds don't need it for public data.
    import uvicorn
    from mcp.server.transport_security import TransportSecuritySettings

    from server.ratelimit import RateLimitMiddleware

    allowed = [h.strip() for h in
               os.environ.get("ALLOWED_HOSTS", "").split(",") if h.strip()]
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=bool(allowed),
        allowed_hosts=allowed)
    http_app = mcp.streamable_http_app(
        json_response=True, stateless_http=True,
        transport_security=security)
    wrapped = RateLimitMiddleware(
        http_app,
        per_minute=int(os.environ.get("RATE_LIMIT_PER_MIN", "120")))
    uvicorn.run(wrapped, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()

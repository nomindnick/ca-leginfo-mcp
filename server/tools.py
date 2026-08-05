"""The seven MCP tools (SPEC §5), as plain functions over Databases.

Every function returns a JSON-able dict wrapped in the response envelope
(extract dates + source note). Error behavior per SPEC: never
empty-and-silent — unknown sections come back with nearest-match
suggestions, archive gaps with an explicit coverage statement.

The MCP wiring lives in server/app.py; keeping the implementations plain
makes them testable against fixture databases without a client.
"""

from __future__ import annotations

import re
import sqlite3
import zlib

from server import history as history_mod
from server import naming
from server.db import Databases, envelope, fmt_session

# current_status values observed in the corpus; everything else
# (Chaptered, Died, Vetoed, Failed…) is no longer pending. Only
# meaningful for the CURRENT session: archived bills keep the last
# status they had when their session ended (12.5k dead bills still say
# "In Committee Process"), so _bill_summary(live=False) never claims
# pending.
PENDING_STATUSES = frozenset({
    "In Committee Process", "In Floor Process", "Passed",
    "Pending Referral", "In Desk Process", "Enrolled",
    "Signed by Governor",
})

# A structural add ("add Chapter 9 (commencing with Section 54950)")
# plausibly reaches sections shortly after its commencing section; the
# archive can't know the chapter's true extent, so we report nearby
# commencing adds as possible matches within this numeric window.
STRUCT_WINDOW = 100.0

_NUM = re.compile(r"^(\d+(?:\.\d+)?)")
_CONS_KEY_ARTICLE = re.compile(r"^Art\. (.+?), Sec\.")


# ---------------------------------------------------------------------------
# shared helpers
# ---------------------------------------------------------------------------

def _numval(section: str) -> float | None:
    m = _NUM.match(section)
    return float(m.group(1)) if m else None


def _ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        return f"{n}th"
    return f"{n}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th') }"


def _chapter_cite(ctype, cyear, cnum, csess="0") -> str | None:
    if not cnum:
        return None
    ex = f", {_ordinal(int(csess))} Ex. Sess." if csess not in (
        None, "", "0") else ""
    if ctype == "CHR":
        return f"Res. Ch. {cnum}, {cyear}{ex}"
    return f"Stats. {cyear}{ex}, Ch. {cnum}"


_BILL_COLS = ("bill_id, session_year, measure_type, measure_num, "
              "measure_state, current_status, current_location, "
              "current_house, chapter_year, chapter_type, chapter_num, "
              "chapter_session_num, latest_bill_version_id")


def _bill_summary(row, live: bool = True) -> dict:
    (bid, sy, mtype, mnum, state, status, loc, house, cyear, ctype, cnum,
     csess, latest) = row
    return {
        "bill_id": bid,
        "measure": f"{mtype} {mnum}",
        "session": fmt_session(sy),
        "state": state,
        "status": status,
        "location": loc,
        "house": house,
        "chapter": _chapter_cite(ctype, cyear, cnum, csess),
        "pending": live and status in PENDING_STATUSES,
        "latest_version_id": latest,
    }


def _authors(con, version_id) -> list[dict]:
    rows = con.execute(
        """SELECT name, house, contribution, primary_author_flg
           FROM bill_version_authors WHERE bill_version_id=?
           ORDER BY primary_author_flg DESC, name""", (version_id,)).fetchall()
    return [{"name": n, "house": h, "contribution": c,
             "primary": p == "Y"} for n, h, c, p in rows]


def _analyses_index(con, bill_id) -> list[dict]:
    rows = con.execute(
        """SELECT a.analysis_id, a.committee_code, a.committee_name,
                  a.house, a.analysis_type, a.analysis_date,
                  a.amendment_date, t.analysis_id IS NOT NULL
           FROM bill_analysis a
           LEFT JOIN analysis_text t ON t.analysis_id = a.analysis_id
           WHERE a.bill_id=? ORDER BY a.analysis_date""",
        (bill_id,)).fetchall()
    return [{"analysis_id": aid, "committee_code": cc, "committee_name": cn,
             "house": h, "type": at, "date": ad, "amendment_date": amd,
             "has_text": bool(ht)}
            for aid, cc, cn, h, at, ad, amd, ht in rows]


def _veto_messages(con, bill_id) -> list[dict]:
    out = []
    for vdate, blob in con.execute(
            """SELECT v.veto_date, t.text_zlib FROM veto_message v
               LEFT JOIN veto_text t
                 ON t.bill_id = v.bill_id AND t.veto_date = v.veto_date
               WHERE v.bill_id=?""", (bill_id,)):
        entry = {"veto_date": vdate}
        if blob is not None:
            entry["text"] = zlib.decompress(blob).decode()
        out.append(entry)
    return out


def _history_actions(con, bill_id) -> list[dict]:
    rows = con.execute(
        """SELECT action_date, action FROM bill_history WHERE bill_id=?
           ORDER BY action_date, CAST(action_sequence AS INTEGER)""",
        (bill_id,)).fetchall()
    return [{"date": d, "action": a} for d, a in rows]


def _versions(con, bill_id) -> list[dict]:
    rows = con.execute(
        """SELECT bill_version_id, version_num, action, action_date
           FROM bill_version WHERE bill_id=?
           ORDER BY CAST(version_num AS INTEGER)""", (bill_id,)).fetchall()
    return [{"version_id": vid, "version_num": vn, "action": a, "date": d}
            for vid, vn, a, d in rows]


def _sections_affected(con, version_id) -> list[dict]:
    rows = con.execute(
        """SELECT action, law_code, section, is_range, range_end, struct
           FROM bill_section_ref WHERE bill_version_id=?""",
        (version_id,)).fetchall()
    return [{"action": a, "code": c, "section": s,
             **({"range_end": re_} if rng else {}),
             **({"adds_structure": st} if st else {})}
            for a, c, s, rng, re_, st in rows]


def _era_notes(arc_con, session_start: str) -> list[str]:
    """Honest per-session coverage limits, from session_coverage."""
    cov = dict(arc_con.execute(
        "SELECT key, value FROM session_coverage WHERE session_year=?",
        (session_start,)))
    if not cov:
        return []
    fmt = fmt_session(f"{session_start}{int(session_start) + 1}")
    notes = []
    if int(session_start) < 1999:
        notes.append(
            f"The {fmt} archive contains primarily chaptered bills; "
            "introduced-but-failed measures may be absent.")
    if cov.get("rows_bill_analysis") == "0":
        notes.append(
            f"The {fmt} archive contains no committee/floor analyses "
            "(electronic analyses begin with the 1993-94 session).")
    if cov.get("rows_bill_history") == "0":
        notes.append(
            f"The {fmt} archive contains no bill history actions.")
    if cov.get("rows_bill_summary_vote") == "0":
        notes.append(
            f"The {fmt} archive contains no vote records "
            "(vote data begins with the 1999-2000 session).")
    if cov.get("veto_texts") == "0":
        notes.append(
            f"The {fmt} archive has no veto message text "
            "(veto texts begin with the 2011-12 session).")
    return notes


def _resolve_code_or_error(code: str) -> tuple[str | None, dict | None]:
    rc = naming.resolve_code(code)
    if rc:
        return rc, None
    return None, {
        "error": f"Unrecognized code name: {code!r}.",
        "suggestions": naming.code_suggestions(code) or
        [f"{c} — {name}" for c, (name, _) in
         sorted(naming.CODE_ALIASES.items())],
    }


def _section_suggestions(con, code: str, key: str) -> list[str]:
    like = [r[0] for r in con.execute(
        """SELECT DISTINCT section_num_norm FROM law_section
           WHERE law_code=? AND section_num_norm LIKE ?
           ORDER BY length(section_num_norm), section_num_norm LIMIT 10""",
        (code, key + "%"))]
    if like:
        return like
    num = _numval(key)
    if num is None:
        return []
    return [r[0] for r in con.execute(
        """SELECT DISTINCT section_num_norm FROM law_section
           WHERE law_code=? AND section_num_norm IS NOT NULL
           ORDER BY abs(CAST(section_num_norm AS REAL) - ?) LIMIT 5""",
        (code, num))]


def _cons_article_sections(con, article: str) -> list[str]:
    prefix = f"Art. {article}, "
    return [r[0] for r in con.execute(
        """SELECT DISTINCT section_num_norm FROM law_section
           WHERE law_code='CONS' AND section_num_norm LIKE ?
           ORDER BY length(section_num_norm), section_num_norm""",
        (prefix + "%",))]


def _parse_section_or_error(con, rc: str, section: str,
                            allow_article: bool = False):
    """Returns (kind, key, error_dict)."""
    kind, key = naming.parse_section(rc, section)
    if kind == "cons_need_article":
        return kind, None, {
            "error": f"Constitution sections are article-scoped; couldn't "
                     f"find an article in {section!r}.",
            "expected_format": 'e.g. "Art. XIII B, Sec. 1" or '
                               '"Article 1, Section 32"',
        }
    if kind == "cons_article" and not allow_article:
        sections = _cons_article_sections(con, key)
        return kind, key, {
            "error": f"Article {key} alone names a whole article; specify a "
                     "section.",
            "article": key,
            "sections_in_article": sections or
            [f"(no Article {key} found in the Constitution)"],
        }
    return kind, key, None


def _hierarchy(con, code: str, key: str, article: str | None) -> list[str]:
    if code == "CONS":
        if not article:
            return []
        row = con.execute(
            "SELECT heading FROM law_toc WHERE law_code='CONS' AND article=?",
            (article,)).fetchone()
        return [row[0]] if row else []
    tp = con.execute(
        """SELECT node_treepath FROM law_toc_sections
           WHERE law_code=? AND section_num IN (?, ?)""",
        (code, key, key + ".")).fetchone()
    if not tp:
        return []
    parts = tp[0].split(".")
    paths = [".".join(parts[:i + 1]) for i in range(len(parts))]
    marks = ",".join("?" * len(paths))
    rows = con.execute(
        f"""SELECT node_treepath, heading FROM law_toc
            WHERE law_code=? AND node_treepath IN ({marks})""",
        (code, *paths)).fetchall()
    rows.sort(key=lambda r: len(r[0].split(".")))
    return [h for _, h in rows if h]


# ---------------------------------------------------------------------------
# tool 1: get_section
# ---------------------------------------------------------------------------

def get_section(dbs: Databases, code: str, section: str) -> dict:
    with dbs.current() as con:
        rc, err = _resolve_code_or_error(code)
        if err:
            return envelope(con, err)
        _kind, key, err = _parse_section_or_error(con, rc, section)
        if err:
            return envelope(con, err)

        rows = con.execute(
            """SELECT section_num, article, history, effective_date,
                      op_statutes, op_chapter, op_section, content_text,
                      active_flg, trans_update
               FROM law_section WHERE law_code=? AND section_num_norm=?""",
            (rc, key)).fetchall()
        if not rows:
            return envelope(con, {
                "error": f"Section {key} not found in the "
                         f"{naming.CODE_ALIASES[rc][0]}.",
                "suggestions": _section_suggestions(con, rc, key),
            })

        article = rows[0][1] if rc == "CONS" else None
        versions = []
        for (snum, art, hist, eff, op_st, op_ch, op_sec, text, active,
             upd) in rows:
            versions.append({
                "text": text,
                "history_note": hist,
                "effective_date": eff,
                "operative_citation": _chapter_cite(
                    "CHP", op_st, op_ch) if op_ch else None,
                "active": active == "Y",
                "last_updated": upd,
            })
        notes = []
        if len(versions) > 1:
            notes.append(
                f"This section has {len(versions)} simultaneous versions "
                "(e.g. future operative dates or competing chapters); all "
                "are returned.")
        return envelope(con, {
            "code": rc,
            "code_name": naming.CODE_ALIASES[rc][0],
            "section": key,
            "hierarchy": _hierarchy(con, rc, key, article),
            "versions": versions,
        }, notes)


# ---------------------------------------------------------------------------
# tool 2: search_sections
# ---------------------------------------------------------------------------

def search_sections(dbs: Databases, query: str, code: str | None = None,
                    limit: int = 10) -> dict:
    limit = max(1, min(int(limit), 50))
    with dbs.current() as con:
        rc = None
        if code:
            rc, err = _resolve_code_or_error(code)
            if err:
                return envelope(con, err)

        notes = []
        sql = ("SELECT ls.law_code, ls.section_num_norm, ls.article, "
               "snippet(law_fts, 0, '>>', '<<', ' … ', 12) "
               "FROM law_fts JOIN law_section ls ON ls.rowid = law_fts.rowid "
               "WHERE law_fts MATCH ?")
        params: list = [query]
        if rc:
            sql += " AND ls.law_code=?"
            params.append(rc)
        sql += " ORDER BY rank LIMIT ?"
        try:
            rows = con.execute(sql, (*params, limit)).fetchall()
        except sqlite3.OperationalError:
            quoted = " ".join(
                f'"{t}"' for t in query.replace('"', " ").split())
            if not quoted:
                return envelope(con, {"error": "Empty search query."})
            params[0] = quoted
            try:
                rows = con.execute(sql, (*params, limit)).fetchall()
                notes.append(
                    "The query contained FTS5 operator syntax that did not "
                    "parse; it was retried as literal quoted terms.")
            except sqlite3.OperationalError as e:
                return envelope(con, {
                    "error": f"Search query could not be parsed: {e}",
                    "hint": "Use plain words or quoted phrases; FTS5 "
                            "operators (AND, OR, NOT, NEAR) are supported.",
                })

        results = [{
            "code": law_code,
            "section": snum,
            "heading": (h[-1] if (h := _hierarchy(
                con, law_code, snum, art)) else None),
            "snippet": snip,
        } for law_code, snum, art, snip in rows]
        if not results:
            notes.append("No matches. FTS5 matches whole words; try fewer "
                         "or broader terms, or quoted phrases.")
        return envelope(con, {"query": params[0], "results": results}, notes)


# ---------------------------------------------------------------------------
# tool 3: bills_affecting_section
# ---------------------------------------------------------------------------

def bills_affecting_section(dbs: Databases, code: str,
                            section: str) -> dict:
    with dbs.current() as con:
        rc, err = _resolve_code_or_error(code)
        if err:
            return envelope(con, err)
        kind, key, err = _parse_section_or_error(con, rc, section,
                                                 allow_article=True)
        if err:
            return envelope(con, err)

        if kind == "cons_article":
            article = key
            refs = con.execute(
                f"""SELECT r.bill_id, r.action, r.section, r.is_range,
                           r.range_end, r.struct, {_BILL_COLS.replace(
                               'bill_id', 'b.bill_id')}
                    FROM bill_section_ref r
                    JOIN bill b ON b.bill_id = r.bill_id
                     AND r.bill_version_id = b.latest_bill_version_id
                    WHERE r.law_code='CONS'
                      AND (r.section=? OR r.section LIKE ?)""",
                (f"Art. {article}", f"Art. {article}, %")).fetchall()
            display_key = f"Art. {article} (whole article)"
        else:
            article = None
            if kind == "cons":
                m = _CONS_KEY_ARTICLE.match(key)
                article = m.group(1) if m else None
                refs = con.execute(
                    f"""SELECT r.bill_id, r.action, r.section, r.is_range,
                               r.range_end, r.struct, {_BILL_COLS.replace(
                                   'bill_id', 'b.bill_id')}
                        FROM bill_section_ref r
                        JOIN bill b ON b.bill_id = r.bill_id
                         AND r.bill_version_id = b.latest_bill_version_id
                        WHERE r.law_code='CONS' AND r.section IN (?, ?)""",
                    (key, f"Art. {article}")).fetchall()
            else:
                refs = con.execute(
                    f"""SELECT r.bill_id, r.action, r.section, r.is_range,
                               r.range_end, r.struct, {_BILL_COLS.replace(
                                   'bill_id', 'b.bill_id')}
                        FROM bill_section_ref r
                        JOIN bill b ON b.bill_id = r.bill_id
                         AND r.bill_version_id = b.latest_bill_version_id
                        WHERE r.law_code=?
                          AND (r.section=? OR r.is_range=1
                               OR r.struct IS NOT NULL)""",
                    (rc, key)).fetchall()
            display_key = key

        qnum = _numval(key) if kind == "plain" else None
        by_bill: dict[str, dict] = {}
        for row in refs:
            action, rsection, is_range, range_end, struct = row[1:6]
            bill_row = row[6:]
            match_kind = None
            if rsection == key or (kind == "cons_article"):
                match_kind = "direct"
                if kind == "cons_article" and rsection == f"Art. {article}":
                    match_kind = "whole_article"
            elif kind == "cons" and rsection == f"Art. {article}":
                match_kind = "whole_article"
            elif is_range and qnum is not None:
                lo, hi = _numval(rsection), _numval(range_end or "")
                if lo is not None and hi is not None and lo <= qnum <= hi:
                    match_kind = "range"
            elif struct and qnum is not None:
                lo = _numval(rsection)
                if lo is not None and 0 < qnum - lo <= STRUCT_WINDOW:
                    match_kind = "structural_add_nearby"
            if match_kind is None:
                continue
            entry = by_bill.setdefault(bill_row[0], {
                "summary": _bill_summary(bill_row),
                "references": [],
            })
            ref_out = {"action": action, "section": rsection,
                       "match": match_kind}
            if struct:
                ref_out["adds_structure"] = struct
            if is_range:
                ref_out["range_end"] = range_end
            entry["references"].append(ref_out)

        bills = []
        for bid, entry in by_bill.items():
            summary = entry["summary"]
            latest_analysis = con.execute(
                "SELECT max(analysis_date) FROM bill_analysis "
                "WHERE bill_id=?", (bid,)).fetchone()[0]
            subject = con.execute(
                "SELECT subject FROM bill_version WHERE bill_version_id=?",
                (summary["latest_version_id"],)).fetchone()
            bills.append({
                **summary,
                "subject": subject[0] if subject else None,
                "latest_analysis_date": latest_analysis,
                "references": entry["references"],
            })
        bills.sort(key=lambda b: (not b["pending"], b["measure"]))

        notes = [("Matches are parsed from each bill's latest-version title: "
                 "'direct' cites the section; 'structural_add_nearby' adds "
                 "a chapter/article commencing within 100 section numbers "
                 "below it (its true extent isn't knowable from the title).")]
        if kind != "cons_article":
            exists = con.execute(
                "SELECT 1 FROM law_section WHERE law_code=? AND "
                "section_num_norm=? LIMIT 1", (rc, key)).fetchone()
            if not exists:
                notes.append(
                    f"{key} is not in current law — a bill listed here may "
                    "be adding it, or the section may have been repealed.")
                if not bills:
                    return envelope(con, {
                        "code": rc, "section": display_key, "bills": [],
                        "suggestions": _section_suggestions(con, rc, key),
                    }, notes)
        if not bills:
            notes.append("No bills in the current session reference this "
                         "section.")
        return envelope(con, {
            "code": rc, "section": display_key, "bills": bills}, notes)


# ---------------------------------------------------------------------------
# tools 4/5 shared bill lookup
# ---------------------------------------------------------------------------

def _find_bills(dbs: Databases, con_current, measure: str,
                session: str | None):
    """Returns (db_label, rows, session_year, error_dict)."""
    pm = naming.parse_measure(measure)
    if not pm:
        return None, [], None, {
            "error": f"Could not parse measure {measure!r}.",
            "expected_format": '"AB 831", "SB 1421", "ACA 13", or a full '
                               'bill_id like "202520260AB13"',
        }
    current_sy = con_current.execute(
        "SELECT value FROM meta WHERE key='session_year'").fetchone()
    current_sy = current_sy[0] if current_sy else None

    # Session precedence: a full bill_id carries its own session; then an
    # explicit session argument; then the current session.
    if pm.get("session_year"):
        sy = pm["session_year"]
    elif session is not None:
        sy = naming.norm_session(session)
        if not sy:
            return None, [], None, {
                "error": f"Could not parse session {session!r}.",
                "expected_format": '"2023-2024", "2023-24", or "2023"',
            }
    else:
        sy = current_sy

    def query(con):
        if "bill_id" in pm:
            return con.execute(
                f"SELECT {_BILL_COLS} FROM bill WHERE bill_id=?",
                (pm["bill_id"],)).fetchall()
        return con.execute(
            f"""SELECT {_BILL_COLS} FROM bill
                WHERE session_year=? AND measure_type=? AND measure_num=?
                ORDER BY bill_id""",
            (sy, pm["type"], pm["num"])).fetchall()

    if sy == current_sy:
        rows = query(con_current)
        db_label = "current"
    else:
        if int(sy[:4]) < 1989:
            return None, [], sy, {
                "error": f"The {fmt_session(sy)} session predates the "
                         "Legislature's electronic records (bill data "
                         "begins with the 1989-90 session).",
            }
        if not dbs.has_archive:
            return None, [], sy, {
                "error": "archive.db is not available on this server; only "
                         f"the current session ({fmt_session(current_sy)}) "
                         "can be searched.",
            }
        with dbs.archive() as arc:
            rows = query(arc)
            era = _era_notes(arc, sy[:4])
        db_label = "archive"

    if not rows:
        name = pm.get("bill_id") or f"{pm['type']} {pm['num']}"
        err = {"error": f"{name} not found in the {fmt_session(sy)} "
                        "session."}
        if db_label == "archive":
            # A miss in a chaptered-only era means "not enacted", not
            # "never existed" — say so (SPEC §5: never empty-and-silent).
            if era:
                err["coverage"] = era
            err["archive_sessions"] = [
                fmt_session(f"{s}{int(s) + 1}")
                for s in dbs.archive_sessions()]
        return db_label, [], sy, err
    return db_label, rows, sy, None


def _votes(con, bill_id) -> list[dict]:
    """Floor/committee vote summaries with motion text — archive.db only
    (SPEC §4 keeps vote tables out of current.db for now)."""
    try:
        rows = con.execute(
            """SELECT v.vote_date_time, v.location_code, m.motion_text,
                      v.ayes, v.noes, v.abstain, v.vote_result
               FROM bill_summary_vote v
               LEFT JOIN bill_motion m ON m.motion_id = v.motion_id
               WHERE v.bill_id=? ORDER BY v.vote_date_time""",
            (bill_id,)).fetchall()
    except sqlite3.OperationalError:  # no vote tables in current.db
        return []
    return [{"date": d, "location": loc, "motion": mo, "ayes": a,
             "noes": n, "abstain": ab, "result": res}
            for d, loc, mo, a, n, ab, res in rows]


def _bill_detail(con, row, db_label: str) -> dict:
    summary = _bill_summary(row, live=db_label == "current")
    latest = summary["latest_version_id"]
    detail = {
        **summary,
        "authors": _authors(con, latest),
        "history": _history_actions(con, summary["bill_id"]),
        "versions": _versions(con, summary["bill_id"]),
        "sections_affected": _sections_affected(con, latest),
        "analyses": _analyses_index(con, summary["bill_id"]),
        "veto_messages": _veto_messages(con, summary["bill_id"]),
    }
    if db_label == "archive":
        detail["votes"] = _votes(con, summary["bill_id"])
    return detail


# ---------------------------------------------------------------------------
# tool 4: get_bill
# ---------------------------------------------------------------------------

def get_bill(dbs: Databases, measure: str,
             session: str | None = None) -> dict:
    with dbs.current() as con:
        db_label, rows, sy, err = _find_bills(dbs, con, measure, session)
        if err:
            return envelope(con, err)

        notes = []
        primary = rows[0]
        extra = rows[1:]
        if extra:
            notes.append(
                f"{len(rows)} measures matched (regular and extraordinary "
                "sessions); the primary result is listed first, the rest "
                "under additional_matches.")

        if db_label == "current":
            detail = _bill_detail(con, primary, db_label)
            extras = [_bill_summary(r) for r in extra]
        else:
            with dbs.archive() as arc:
                detail = _bill_detail(arc, primary, db_label)
                extras = [_bill_summary(r, live=False) for r in extra]
                notes.extend(_era_notes(arc, sy[:4]))
            if detail["status"] is None:
                notes.append("Status/location fields are not populated in "
                             "this archive era; see the history actions "
                             "and chapter fields instead.")
        if detail["versions"]:
            notes.append("Versions are listed newest first (Legislative "
                         "Counsel version numbers count down from 99).")

        payload = {"bill": detail, "from": db_label}
        if extras:
            payload["additional_matches"] = extras
        return envelope(con, payload, notes)


# ---------------------------------------------------------------------------
# tool 5: get_bill_analyses
# ---------------------------------------------------------------------------

def get_bill_analyses(dbs: Databases, measure: str | None = None,
                      session: str | None = None,
                      analysis_id: str | int | None = None) -> dict:
    with dbs.current() as con:
        if analysis_id is not None:
            return _analysis_text(dbs, con, str(analysis_id).strip())
        if not measure:
            return envelope(con, {
                "error": "Provide a measure (e.g. \"AB 831\") for an index "
                         "of analyses, or an analysis_id for full text.",
            })
        db_label, rows, sy, err = _find_bills(dbs, con, measure, session)
        if err:
            return envelope(con, err)
        notes = []
        out = []
        source = con
        if db_label == "archive":
            with dbs.archive() as arc:
                for row in rows:
                    out.append({
                        "bill": _bill_summary(row, live=False),
                        "analyses": _analyses_index(arc, row[0]),
                    })
                notes.extend(_era_notes(arc, sy[:4]))
        else:
            for row in rows:
                out.append({
                    "bill": _bill_summary(row),
                    "analyses": _analyses_index(source, row[0]),
                })
        notes.append("Fetch any analysis's full text by calling this tool "
                     "with its analysis_id.")
        payload = dict(out[0])
        if len(out) > 1:
            payload["additional_matches"] = out[1:]
            notes.append(
                f"{len(out)} measures matched (regular and extraordinary "
                "sessions); the primary result is listed first.")
        return envelope(con, {**payload, "from": db_label}, notes)


def _analysis_text(dbs: Databases, con_current, aid: str) -> dict:
    sources = [("current", None)]
    if dbs.has_archive:
        sources.append(("archive", None))
    for label, _ in sources:
        if label == "current":
            found = _fetch_analysis(con_current, aid, live=True)
        else:
            with dbs.archive() as arc:
                found = _fetch_analysis(arc, aid, live=False)
        if found:
            found["from"] = label
            notes = []
            if found.pop("_no_text", False):
                notes.append("No extracted text is stored for this "
                             "analysis (the source document was missing or "
                             "unconvertible).")
            return envelope(con_current, found, notes)
    where = ("the current session or the archive" if dbs.has_archive
             else "the current session (archive.db is not available on "
                  "this server, so prior sessions were not searched)")
    return envelope(con_current, {
        "error": f"analysis_id {aid} not found in {where}.",
    })


def _fetch_analysis(con, aid: str, live: bool = True) -> dict | None:
    row = con.execute(
        """SELECT a.analysis_id, a.bill_id, a.committee_code,
                  a.committee_name, a.house, a.analysis_type,
                  a.analysis_date, a.amendment_date, t.text_zlib
           FROM bill_analysis a
           LEFT JOIN analysis_text t ON t.analysis_id = a.analysis_id
           WHERE a.analysis_id=?""", (aid,)).fetchone()
    if not row:
        return None
    (aid_, bid, cc, cn, house, atype, adate, amdate, blob) = row
    bill = con.execute(
        f"SELECT {_BILL_COLS} FROM bill WHERE bill_id=?", (bid,)).fetchone()
    out = {
        "analysis_id": aid_,
        "bill": _bill_summary(bill, live=live) if bill else {"bill_id": bid},
        "committee_code": cc,
        "committee_name": cn,
        "house": house,
        "type": atype,
        "date": adate,
        "amendment_date": amdate,
    }
    if blob is None:
        out["text"] = None
        out["_no_text"] = True
    else:
        out["text"] = zlib.decompress(blob).decode()
    return out


# ---------------------------------------------------------------------------
# tool 6: get_legislative_history
# ---------------------------------------------------------------------------

def get_legislative_history(dbs: Databases, code: str,
                            section: str) -> dict:
    with dbs.current() as con:
        rc, err = _resolve_code_or_error(code)
        if err:
            return envelope(con, err)
        _kind, key, err = _parse_section_or_error(con, rc, section)
        if err:
            return envelope(con, err)

        rows = con.execute(
            """SELECT section_num, article, history FROM law_section
               WHERE law_code=? AND section_num_norm=?""",
            (rc, key)).fetchall()
        if not rows:
            return envelope(con, {
                "error": f"Section {key} not found in the "
                         f"{naming.CODE_ALIASES[rc][0]}.",
                "suggestions": _section_suggestions(con, rc, key),
            })

        article = rows[0][1] if rc == "CONS" else None
        history_notes = list(dict.fromkeys(
            r[2] for r in rows if r[2]))

        events: list[history_mod.HistoryEvent] = []
        seen: set[tuple] = set()
        for note in history_notes:
            for ev in history_mod.parse_history(note).events:
                if ev.key() not in seen:
                    seen.add(ev.key())
                    events.append(ev)

        notes = [("A section's history note names its most recent "
                 "amendment (plus, in parentheticals, the amendment it "
                 "modified); enacted_bills_citing_section reconstructs "
                 "the earlier lineage from archived bill titles "
                 "(1989-present, title-based).")]
        if not dbs.has_archive:
            notes.append("archive.db is not available on this server — "
                         "only current-session chapters can be resolved.")
        if not events and history_notes:
            notes.append("No Stats./Res.Ch./initiative citation was "
                         "recognized in the history note; the raw note is "
                         "included for manual review.")
        if not history_notes:
            notes.append("This section's law record carries no history "
                         "note.")

        resolved = [_resolve_event(dbs, con, ev, notes) for ev in events]
        citing = _bills_citing(dbs, con, rc, key, article)

        return envelope(con, {
            "code": rc,
            "section": key,
            "history_notes": history_notes,
            "events": resolved,
            "enacted_bills_citing_section": citing,
        }, notes)


def _resolve_event(dbs: Databases, con_current,
                   ev: history_mod.HistoryEvent,
                   notes: list[str]) -> dict:
    base = {"citation": ev.citation, "kind": ev.kind, "role": ev.role}
    if ev.kind == "initiative":
        return {
            **base,
            "proposition": ev.proposition,
            "date": ev.date,
            "resolution": "adopted_by_initiative",
            "note": f"Adopted by voter initiative (Proposition "
                    f"{ev.proposition}{', ' + ev.date if ev.date else ''}). "
                    "There is no authoring bill or committee analysis; see "
                    "the Secretary of State's ballot pamphlet.",
        }

    ctype = "CHR" if ev.kind == "resolution_chapter" else "CHP"
    if ev.proposition:
        base["proposition"] = ev.proposition

    years = [y for y in (ev.year, ev.year_alt) if y]
    for year in years:
        resolved = _lookup_chapter(dbs, con_current, year, ev.chapter,
                                   ctype, ev.ex_session)
        if resolved:
            resolved = {**base, "resolution": "resolved", **resolved}
            if ev.measure_hint and \
                    resolved["bill"]["measure"] != ev.measure_hint:
                resolved["warning"] = (
                    f"The history note's parenthetical names "
                    f"{ev.measure_hint}, but the chapter resolves to "
                    f"{resolved['bill']['measure']} — flagging for review.")
            return resolved

    if years and all(y < 1989 for y in years):
        return {**base, "resolution": "predates_electronic_records",
                "note": "This citation predates the Legislature's "
                        "electronic records (bill data begins 1989; "
                        "analyses begin 1993). Consult the State Archives "
                        "or a legislative-intent service for this era."}
    if not dbs.has_archive:
        return {**base, "resolution": "unresolved",
                "note": "archive.db not available on this server."}
    return {**base, "resolution": "unresolved",
            "note": f"No {'resolution ' if ctype == 'CHR' else ''}chapter "
                    f"{ev.chapter} of {'/'.join(map(str, years))} matched "
                    "a bill record."}


def _chapter_bill(con, year: int, chapter: int, ctype: str,
                  ex_session: int) -> list:
    """All bills carrying a chapter designation — the key is not unique in
    the real archive (adjacent sessions' December organizing resolutions
    share 'Res. Ch. 1' of an even year, and pubinfo has a few duplicate
    chapter records), so callers surface ambiguity instead of guessing."""
    return con.execute(
        f"""SELECT {_BILL_COLS} FROM bill
            WHERE chapter_year=? AND chapter_num=? AND chapter_type=?
              AND chapter_session_num=? ORDER BY bill_id""",
        (str(year), str(chapter), ctype, str(ex_session))).fetchall()


def _ambiguity_note(rows) -> str | None:
    if len(rows) <= 1:
        return None
    others = ", ".join(r[0] for r in rows[1:])
    return (f"{len(rows)} bill records carry this chapter designation "
            f"(returning {rows[0][0]}; also {others}). Adjacent sessions' "
            "resolutions can share a chapter number, and pubinfo contains "
            "a few duplicate chapter records — verify against the official "
            "Statutes.")


def _lookup_chapter(dbs: Databases, con_current, year: int, chapter: int,
                    ctype: str, ex_session: int) -> dict | None:
    """Chapter -> bill payload, searching current.db then the archive.

    Both stores are always consulted (indexed, cheap): routing by nominal
    session years is wrong for December chapters — a new Legislature
    chapters its organizing resolution in the calendar year before its
    session's nominal years.
    """
    rows = _chapter_bill(con_current, year, chapter, ctype, ex_session)
    if rows:
        payload = _chapter_payload(con_current, rows[0], "current")
        if note := _ambiguity_note(rows):
            payload["warning"] = note
        return payload
    if dbs.has_archive and year >= 1988:
        with dbs.archive() as arc:
            rows = _chapter_bill(arc, year, chapter, ctype, ex_session)
            if rows:
                payload = _chapter_payload(arc, rows[0], "archive")
                era = _era_notes(arc, rows[0][1][:4])
                if era:
                    payload["coverage"] = era
                if note := _ambiguity_note(rows):
                    payload["warning"] = note
                return payload
    return None


def _chapter_payload(con, row, db_label: str) -> dict:
    summary = _bill_summary(row, live=db_label == "current")
    return {
        "bill": summary,
        "authors": _authors(con, summary["latest_version_id"]),
        "analyses": _analyses_index(con, summary["bill_id"]),
        "veto_messages": _veto_messages(con, summary["bill_id"]),
        "from": db_label,
    }


def _bills_citing(dbs: Databases, con_current, rc: str, key: str,
                  article: str | None, cap: int = 100) -> list[dict]:
    """Enacted bills whose final title cites the section — the archive-wide
    lineage supplement (title-based, 1989-present)."""
    sections = [key]
    if rc == "CONS" and article:
        sections.append(f"Art. {article}")
    marks = ",".join("?" * len(sections))
    sql = f"""SELECT r.bill_id, b.session_year, b.measure_type,
                     b.measure_num, b.chapter_year, b.chapter_type,
                     b.chapter_num, b.chapter_session_num, r.action,
                     r.section
              FROM bill_section_ref r
              JOIN bill b ON b.bill_id = r.bill_id
               AND r.bill_version_id = b.latest_bill_version_id
              WHERE r.law_code=? AND r.section IN ({marks})
                AND b.chapter_num IS NOT NULL
                AND b.chapter_type IN ('CHP', 'CHR')"""
    params = (rc, *sections)
    rows = list(con_current.execute(sql, params))
    if dbs.has_archive:
        with dbs.archive() as arc:
            rows.extend(arc.execute(sql, params))

    by_bill: dict[str, dict] = {}
    for (bid, sy, mtype, mnum, cyear, ctype, cnum, csess, action,
         rsection) in rows:
        entry = by_bill.setdefault(bid, {
            "bill_id": bid,
            "measure": f"{mtype} {mnum}",
            "session": fmt_session(sy),
            "chapter": _chapter_cite(ctype, cyear, cnum, csess),
            "chapter_year": int(cyear) if cyear else None,
            "actions": [],
        })
        label = action + (" (whole article)"
                          if rsection != key else "")
        if label not in entry["actions"]:
            entry["actions"].append(label)
    out = sorted(by_bill.values(),
                 key=lambda e: (e["chapter_year"] or 0), reverse=True)
    for e in out:
        e.pop("chapter_year", None)
    return out[:cap]


# ---------------------------------------------------------------------------
# tool 7: chapter_to_bill
# ---------------------------------------------------------------------------

def chapter_to_bill(dbs: Databases, year: int, chapter: int,
                    kind: str = "statutes", ex_session: int = 0) -> dict:
    ctype = {"statutes": "CHP", "resolution": "CHR"}.get(kind)
    with dbs.current() as con:
        if ctype is None:
            return envelope(con, {
                "error": f"Unknown chapter kind {kind!r}; use 'statutes' "
                         "or 'resolution'."})
        try:
            year_i, chapter_i = int(year), int(chapter)
        except (TypeError, ValueError):
            return envelope(con, {
                "error": "year and chapter must be integers."})

        if year_i < 1989:
            return envelope(con, {
                "error": f"Chapter records for {year_i} predate the "
                         "Legislature's electronic records (bill data "
                         "begins with the 1989-90 session).",
            })

        payload = _lookup_chapter(dbs, con, year_i, chapter_i, ctype,
                                  ex_session)
        if payload:
            notes = payload.pop("coverage", [])
            return envelope(con, {
                "chapter": _chapter_cite(ctype, str(year_i),
                                         str(chapter_i), str(ex_session)),
                **payload,
            }, notes)

        # Not found: check the other session_num values (both stores)
        # before giving up.
        others_sql = """SELECT chapter_session_num, bill_id FROM bill
                        WHERE chapter_year=? AND chapter_num=?
                          AND chapter_type=?"""
        params = (str(year_i), str(chapter_i), ctype)
        others = con.execute(others_sql, params).fetchall()
        if dbs.has_archive:
            with dbs.archive() as arc:
                others.extend(arc.execute(others_sql, params))
        hint = [f"chapter exists in extraordinary session "
                f"{s} (bill {b}); pass ex_session={s}"
                for s, b in others if s != str(ex_session)]
        label = "Res. Ch." if ctype == "CHR" else "Ch."
        return envelope(con, {
            "error": f"{label} {chapter_i}, {year_i}"
                     f"{f' ({_ordinal(ex_session)} Ex. Sess.)' if ex_session else ''}"
                     " did not match any bill.",
            **({"hint": hint} if hint else {}),
            "coverage": _coverage_statement(dbs, con),
        })


def _coverage_statement(dbs: Databases, con_current) -> str:
    sy = con_current.execute(
        "SELECT value FROM meta WHERE key='session_year'").fetchone()
    current = fmt_session(sy[0]) if sy else "unknown"
    if dbs.has_archive:
        sessions = dbs.archive_sessions()
        if sessions:
            first, last = sessions[0], sessions[-1]
            return (f"Archive covers the {first}-{int(first) + 1} through "
                    f"{last}-{int(last) + 1} sessions; current.db covers "
                    f"{current}. Chapters before 1989 predate electronic "
                    "records.")
    return (f"Only current.db ({current}) is available on this server; "
            "prior sessions cannot be resolved.")

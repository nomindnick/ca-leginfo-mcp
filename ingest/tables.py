"""pubinfo table definitions, transcribed from the load scripts in
pubinfo_load.zip (capublic schema).

Two freshness groups drive the nightly overlay logic (SPEC §3/§6):

- LAW tables are TRUNCATE+reload artifacts — always loaded wholesale from a
  single source zip, never partially merged.
- BILL tables are complete in the full session zip and in the nightly
  ``pubinfo_daily_[Day].zip``; loaded wholesale from whichever is fresher.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Table:
    dat: str  # e.g. "LAW_SECTION_TBL" -> LAW_SECTION_TBL.dat in the zip
    name: str  # SQLite table name
    columns: tuple[str, ...]


LAW_TABLES: tuple[Table, ...] = (
    Table("CODES_TBL", "codes", ("code", "title")),
    Table(
        "LAW_SECTION_TBL",
        "law_section",
        (
            "id", "law_code", "section_num", "op_statutes", "op_chapter",
            "op_section", "effective_date", "version_id", "division", "title",
            "part", "chapter", "article", "history", "lob_file", "active_flg",
            "trans_uid", "trans_update",
        ),
    ),
    Table(
        "LAW_TOC_TBL",
        "law_toc",
        (
            "law_code", "division", "title", "part", "chapter", "article",
            "heading", "active_flg", "trans_uid", "trans_update",
            "node_sequence", "node_level", "node_position", "node_treepath",
            "contains_law_sections", "history_note", "op_statutes",
            "op_chapter", "op_section",
        ),
    ),
    Table(
        "LAW_TOC_SECTIONS_TBL",
        "law_toc_sections",
        (
            "id", "law_code", "node_treepath", "section_num", "section_order",
            "title", "op_statutes", "op_chapter", "op_section", "trans_uid",
            "trans_update", "version_id", "seq_num",
        ),
    ),
)

BILL_TABLES: tuple[Table, ...] = (
    Table(
        "BILL_TBL",
        "bill",
        (
            "bill_id", "session_year", "session_num", "measure_type",
            "measure_num", "measure_state", "chapter_year", "chapter_type",
            "chapter_session_num", "chapter_num", "latest_bill_version_id",
            "active_flg", "trans_uid", "trans_update", "current_location",
            "current_secondary_loc", "current_house", "current_status",
            "days_31st_in_print",
        ),
    ),
    Table(
        "BILL_VERSION_TBL",
        "bill_version",
        (
            "bill_version_id", "bill_id", "version_num", "action_date",
            "action", "request_num", "subject", "vote_required",
            "appropriation", "fiscal_committee", "local_program",
            "substantive_changes", "urgency", "tax_levy", "lob_file",
            "active_flg", "trans_uid", "trans_update",
        ),
    ),
    Table(
        "BILL_VERSION_AUTHORS_TBL",
        "bill_version_authors",
        (
            "bill_version_id", "type", "house", "name", "contribution",
            "committee_members", "active_flg", "trans_uid", "trans_update",
            "primary_author_flg",
        ),
    ),
    Table(
        "BILL_HISTORY_TBL",
        "bill_history",
        (
            "bill_id", "bill_history_id", "action_date", "action",
            "trans_uid", "trans_update", "action_sequence", "action_code",
            "action_status", "primary_location", "secondary_location",
            "ternary_location", "end_status",
        ),
    ),
    Table(
        "BILL_ANALYSIS_TBL",
        "bill_analysis",
        (
            "analysis_id", "bill_id", "house", "analysis_type",
            "committee_code", "committee_name", "amendment_author",
            "analysis_date", "amendment_date", "page_num", "lob_file",
            "released_floor", "active_flg", "trans_uid", "trans_update",
        ),
    ),
    Table(
        "VETO_MESSAGE_TBL",
        "veto_message",
        ("bill_id", "veto_date", "lob_file", "trans_uid", "trans_update"),
    ),
)

# Vote data lives only in archive.db for now (SPEC §4); bill_motion is
# included so summary/detail votes are interpretable (motion_id -> text).
ARCHIVE_ONLY_TABLES: tuple[Table, ...] = (
    Table(
        "BILL_DETAIL_VOTE_TBL",
        "bill_detail_vote",
        (
            "bill_id", "location_code", "legislator_name", "vote_date_time",
            "vote_date_seq", "vote_code", "motion_id", "trans_uid",
            "trans_update", "member_order", "session_date", "speaker",
        ),
    ),
    Table(
        "BILL_SUMMARY_VOTE_TBL",
        "bill_summary_vote",
        (
            "bill_id", "location_code", "vote_date_time", "vote_date_seq",
            "motion_id", "ayes", "noes", "abstain", "vote_result",
            "trans_uid", "trans_update", "file_item_num", "file_location",
            "display_lines", "session_date",
        ),
    ),
    Table(
        "BILL_MOTION_TBL",
        "bill_motion",
        ("motion_id", "motion_text", "trans_uid", "trans_update"),
    ),
)

# Everything a session archive can carry (loaders tolerate absences —
# eras differ; the per-session coverage matrix records reality).
ARCHIVE_TABLES: tuple[Table, ...] = BILL_TABLES + ARCHIVE_ONLY_TABLES

ALL_TABLES: tuple[Table, ...] = LAW_TABLES + BILL_TABLES

LAW_DAT_NAMES = frozenset(f"{t.dat}.dat" for t in LAW_TABLES)

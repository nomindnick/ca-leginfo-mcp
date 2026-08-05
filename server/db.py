"""Read-only access to the two corpus artifacts (SPEC §3).

current.db is mandatory; archive.db is optional — history tools degrade
with an explicit coverage note instead of failing (SPEC §5 error
behavior: never empty-and-silent).

Connections are opened read-only per call (``mode=ro`` URI) and closed
immediately. The nightly pipeline replaces current.db by atomic rename;
short-lived connections mean the server picks up a swapped file without
restarting, and read-only mode guarantees the server can never corrupt
an artifact.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

SOURCE_NOTE = (
    "Derived from the California Legislature's public bulk-data downloads "
    "(downloads.leginfo.legislature.ca.gov). Unofficial convenience mirror: "
    "statute text refreshes weekly, bill data nightly. Verify against the "
    "official publication before citing in a filing."
)

CURRENT_DB_ENV = "CA_LEGINFO_CURRENT_DB"
ARCHIVE_DB_ENV = "CA_LEGINFO_ARCHIVE_DB"


class Databases:
    """Paths to the artifacts plus per-call connection factories."""

    def __init__(self, current: Path, archive: Path | None = None):
        self.current_path = Path(current)
        self.archive_path = Path(archive) if archive else None

    @classmethod
    def from_env(cls) -> Databases:
        current = Path(os.environ.get(CURRENT_DB_ENV, "current.db"))
        archive_raw = os.environ.get(ARCHIVE_DB_ENV, "archive.db")
        # Keep the path even if the file is missing right now:
        # has_archive re-checks existence per call, so an archive.db that
        # finishes downloading after boot is picked up without a restart.
        # An explicitly empty env var disables the archive.
        archive = Path(archive_raw) if archive_raw else None
        return cls(current, archive)

    @property
    def has_archive(self) -> bool:
        return self.archive_path is not None and self.archive_path.exists()

    @contextmanager
    def current(self):
        con = _connect_ro(self.current_path)
        try:
            yield con
        finally:
            con.close()

    @contextmanager
    def archive(self):
        if not self.has_archive:
            raise FileNotFoundError("archive.db not available")
        con = _connect_ro(self.archive_path)
        try:
            yield con
        finally:
            con.close()

    def archive_sessions(self) -> list[str]:
        """Session start years present in archive.db, e.g. ['1989', ...]."""
        if not self.has_archive:
            return []
        import json

        with self.archive() as con:
            row = con.execute(
                "SELECT value FROM meta WHERE key='sessions'").fetchone()
        return json.loads(row[0]) if row else []


def _connect_ro(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise FileNotFoundError(f"database not found: {path}")
    uri = f"{path.resolve().as_uri()}?mode=ro"
    return sqlite3.connect(uri, uri=True)


def fmt_session(session_year: str | None) -> str | None:
    """'20252026' -> '2025-2026'."""
    if session_year and len(session_year) == 8:
        return f"{session_year[:4]}-{session_year[4:]}"
    return session_year


def envelope(con_current: sqlite3.Connection, payload: dict,
             notes: list[str] | None = None) -> dict:
    """The response envelope every tool carries (SPEC §5): extract dates,
    session, and the unofficial-source note, merged over the payload."""
    meta = dict(con_current.execute(
        "SELECT key, value FROM meta WHERE key IN "
        "('law_extract_date', 'bill_extract_date', 'session_year')"))
    out = {
        "law_extract_date": meta.get("law_extract_date") or None,
        "bill_extract_date": meta.get("bill_extract_date") or None,
        "current_session": fmt_session(meta.get("session_year")),
        "source": SOURCE_NOTE,
    }
    if notes:
        out["notes"] = list(notes)
    out.update(payload)
    return out

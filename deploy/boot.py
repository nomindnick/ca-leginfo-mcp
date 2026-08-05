"""Server boot: sync artifacts from R2, then serve MCP over HTTP.

Boot order: current.db is mandatory (fail fast without it); archive.db
is large (4.7 GB) and optional — if the download fails the server starts
anyway and the history tools state the gap (server/db.py re-checks the
path per call, so a later successful sync is picked up live).

A background thread re-checks current.db every REFRESH_HOURS (default 6)
with a single conditional HEAD; when the nightly job has uploaded a new
artifact it's swapped in atomically (tmp + os.replace) — the server's
per-call read-only connections never notice. archive.db is only synced
at boot: it changes rarely (pipeline fixes, biennial rollover), and a
Railway redeploy re-runs boot.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path

from deploy import r2sync
from server import app as server_app
from server.db import ARCHIVE_DB_ENV, CURRENT_DB_ENV

log = logging.getLogger("deploy.boot")


def sync(data_dir: Path) -> tuple[Path, Path]:
    s3 = r2sync.client()
    current = data_dir / "current.db"
    archive = data_dir / "archive.db"
    r2sync.download(s3, "current.db", current)  # raises if absent: fatal
    try:
        r2sync.download(s3, "archive.db", archive)
    except Exception as e:  # noqa: BLE001 — degrade, don't die
        log.error("archive.db sync failed (%r) — serving without the "
                  "archive; history tools will state the gap", e)
    return current, archive


def refresher(data_dir: Path, interval_hours: float) -> None:
    while True:
        time.sleep(interval_hours * 3600)
        try:
            s3 = r2sync.client()
            if r2sync.download(s3, "current.db", data_dir / "current.db"):
                log.info("current.db refreshed from R2")
            if not (data_dir / "archive.db").exists():
                r2sync.download(s3, "archive.db", data_dir / "archive.db")
        except Exception as e:  # noqa: BLE001 — transient; retry next tick
            log.warning("refresh failed: %r", e)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    data_dir = Path(os.environ.get("DATA_DIR", "/data"))
    data_dir.mkdir(parents=True, exist_ok=True)

    current, archive = sync(data_dir)
    os.environ[CURRENT_DB_ENV] = str(current)
    os.environ[ARCHIVE_DB_ENV] = str(archive)

    threading.Thread(
        target=refresher,
        args=(data_dir, float(os.environ.get("REFRESH_HOURS", "6"))),
        daemon=True, name="r2-refresher").start()

    server_app.main(["--transport", "http",
                     "--host", os.environ.get("HOST", "0.0.0.0"),
                     "--port", os.environ.get("PORT", "8000")])


if __name__ == "__main__":
    main()

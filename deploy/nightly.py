"""Nightly current.db rebuild (SPEC §3): stateless and drift-free.

Every run: pick the freshest ``pubinfo_daily_[Day].zip`` (complete bill
data, ~1 GB, written ~04:30 UTC after each legislative day), reuse the
cached Sunday ``pubinfo_YYYY.zip`` for law tables (conditional GET — only
re-downloaded when the Legislature refreshes it), grab the matching small
``pubinfo_[Day].zip`` incremental (in case it ever ships LAW tables —
expected around January), build a fresh DB, gate it with the sanity
checks against last night's artifact, and upload to R2 on PASS. Never
merges into an existing DB.

Freshness probing HEADs all seven daily names and takes the newest
Last-Modified — immune to timezone/day-boundary arithmetic. The session
zip name is auto-detected from the site index (``pubinfo_2025.zip`` →
``pubinfo_2027.zip`` at rollover) unless CA_LEGINFO_LAW_ZIP pins it.

Exit code 0 only on a gated, uploaded artifact — Railway surfaces cron
failures.
"""

from __future__ import annotations

import email.utils
import json
import logging
import os
import re
import sys
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

from deploy import r2sync
from ingest.build import build_current_db
from ingest.sanity import check_db

log = logging.getLogger("deploy.nightly")

BASE = "https://downloads.leginfo.legislature.ca.gov"
DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
STALE_HOURS = 40  # freshest daily older than this → warn (holiday gaps)
_UA = {"User-Agent": "ca-leginfo-mcp ingest (bulk-data channel)"}


def _head(url: str) -> tuple[int, dict]:
    req = urllib.request.Request(url, method="HEAD", headers=_UA)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {})


def _last_modified(headers: dict) -> float | None:
    lm = headers.get("Last-Modified")
    return email.utils.parsedate_to_datetime(lm).timestamp() if lm else None


def freshest_daily() -> tuple[str, float]:
    """(day name, last-modified epoch) of the newest daily zip."""
    best: tuple[str, float] | None = None
    for day in DAYS:
        status, headers = _head(f"{BASE}/pubinfo_daily_{day}.zip")
        lm = _last_modified(headers) if status == 200 else None
        if lm and (best is None or lm > best[1]):
            best = (day, lm)
    if best is None:
        raise RuntimeError("no pubinfo_daily_*.zip reachable")
    age_h = (time.time() - best[1]) / 3600
    log.info("freshest daily: %s (%.1f h old)", best[0], age_h)
    if age_h > STALE_HOURS:
        log.warning("freshest daily is %.0f h old — holiday gap or "
                    "publisher outage; building anyway", age_h)
    return best


def _newest_law_zip(index_html: str) -> str:
    years = re.findall(r'href="pubinfo_(\d{4})\.zip"', index_html)
    if not years:
        raise RuntimeError("could not find any pubinfo_YYYY.zip in index")
    return f"pubinfo_{max(years)}.zip"


def detect_law_zip_name() -> str:
    """Newest pubinfo_YYYY.zip on the site index (session rollover-proof)."""
    pinned = os.environ.get("CA_LEGINFO_LAW_ZIP")
    if pinned:
        return pinned
    req = urllib.request.Request(BASE + "/", headers=_UA)
    with urllib.request.urlopen(req, timeout=60) as resp:
        return _newest_law_zip(resp.read().decode("utf-8", errors="replace"))


def fetch(name: str, dest: Path, *, conditional: bool = False) -> bool:
    """Streaming download of BASE/name -> dest (tmp + atomic replace).

    With ``conditional``, sends If-Modified-Since from the sidecar of the
    previous fetch and returns False on 304 — that's what makes the
    Sunday law zip a weekly download instead of a nightly one.
    """
    url = f"{BASE}/{name}"
    sidecar = dest.with_name(dest.name + ".lastmod")
    headers = dict(_UA)
    if conditional and dest.exists() and sidecar.exists():
        headers["If-Modified-Since"] = sidecar.read_text().strip()
    req = urllib.request.Request(url, headers=headers)
    tmp = dest.with_name(dest.name + ".tmp")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            with open(tmp, "wb") as f:
                while chunk := resp.read(1 << 22):
                    f.write(chunk)
            lm = resp.headers.get("Last-Modified", "")
    except urllib.error.HTTPError as e:
        tmp.unlink(missing_ok=True)
        if e.code == 304:
            log.info("%s not modified — using cached copy", name)
            return False
        raise
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, dest)
    if lm:
        sidecar.write_text(lm)
    log.info("downloaded %s (%d MB)", name,
             dest.stat().st_size // 1_000_000)
    return True


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    work = Path(os.environ.get("DATA_DIR", "/data"))
    work.mkdir(parents=True, exist_ok=True)
    t0 = time.time()

    law_name = detect_law_zip_name()
    law_zip = work / law_name
    fetch(law_name, law_zip, conditional=True)

    day, _ = freshest_daily()
    daily_zip = work / "daily.zip"
    fetch(f"pubinfo_daily_{day}.zip", daily_zip)
    inc_zip = work / "incremental.zip"
    fetch(f"pubinfo_{day}.zip", inc_zip)

    out_tmp = work / "current.new.db"
    out_tmp.unlink(missing_ok=True)
    report = build_current_db(
        law_zip, out_tmp, bill_zip=daily_zip, incremental_zip=inc_zip)

    previous = work / "current.db"
    sanity = check_db(out_tmp, previous if previous.exists() else None)
    for c in sanity.checks:
        if not c.ok:
            log.warning("[%s] %s: %s", c.level, c.name, c.detail)
    if not sanity.ok:
        log.error("SANITY GATE FAILED — artifact not uploaded")
        print(sanity.to_json())
        return 1
    log.info("sanity PASS (%d warnings)", len(sanity.warnings))

    s3 = r2sync.client()
    r2sync.upload(s3, out_tmp, "current.db")
    stamp = datetime.now(UTC).strftime("%Y%m%d")
    body = json.dumps({
        "report": json.loads(report.to_json()),
        "sanity": json.loads(sanity.to_json()),
    }).encode()
    s3.put_object(Bucket=r2sync.bucket(),
                  Key=f"reports/current-{stamp}.json", Body=body)

    os.replace(out_tmp, previous)  # becomes tomorrow's non-regression base
    daily_zip.unlink(missing_ok=True)  # ~1 GB; no reason to keep it
    log.info("nightly complete in %.0f s (law %s, bills %s)",
             time.time() - t0, report.law_extract_date,
             report.bill_extract_date)
    return 0


if __name__ == "__main__":
    sys.exit(main())

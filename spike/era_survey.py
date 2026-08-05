"""Inventory each session archive: tables present, analysis count and
formats, law tables presence.

Usage: python3 era_survey.py <zip> [<zip> ...]
"""

import random
import sys
import zipfile

from analyses import detect


def survey(path: str) -> None:
    rng = random.Random(7)
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        dats = sorted(n for n in names if n.endswith(".dat"))
        analysis_lobs = [n for n in names
                         if n.startswith("BILL_ANALYSIS_TBL_")]
        version_lobs = [n for n in names if n.startswith("BILL_VERSION_TBL_")]
        formats: dict[str, int] = {}
        for n in rng.sample(analysis_lobs, min(40, len(analysis_lobs))):
            k = detect(zf.read(n)[:4096])
            formats[k] = formats.get(k, 0) + 1
        vformats: dict[str, int] = {}
        for n in rng.sample(version_lobs, min(10, len(version_lobs))):
            head = zf.read(n)[:200].lstrip()
            k = "xml" if head.startswith(b"<?xml") or head.startswith(b"<")\
                else "other"
            vformats[k] = vformats.get(k, 0) + 1
        session = path.rsplit("_", 1)[1].removesuffix(".zip")
        tables = ",".join(d.removesuffix("_TBL.dat") for d in dats)
        print(f"{session}: analyses={len(analysis_lobs)} "
              f"formats={formats or '-'} versions={len(version_lobs)} "
              f"({vformats}) tables=[{tables}]")


if __name__ == "__main__":
    for p in sys.argv[1:]:
        survey(p)

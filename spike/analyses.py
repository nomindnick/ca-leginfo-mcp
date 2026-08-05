"""Extract text from bill analysis lobs across all three era formats.

Dispatch on file magic, never on era: HTML (1990s), legacy binary .doc
(2000s, via LibreOffice headless), .docx (2010s+, via stdlib zipfile).

Usage: python3 analyses.py sample <session.zip> <n>   # test n random lobs
"""

import html
import random
import re
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path


def detect(data: bytes) -> str:
    if data[:4] == b"\xd0\xcf\x11\xe0":
        return "doc"
    if data[:2] == b"PK":
        return "docx"
    head = data[:2048].lstrip()[:200].lower()
    if head.startswith(b"<!doctype") or b"<html" in head or b"<body" in head:
        return "html"
    if data[:5] == b"{\\rtf":
        return "rtf"
    return "text"


def extract_docx(data: bytes) -> str:
    import io
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        xml = z.read("word/document.xml").decode("utf-8", errors="replace")
    # Field instruction text (MERGEFIELD codes etc.) is markup, not content.
    xml = re.sub(r"<w:instrText[^>]*>.*?</w:instrText>", "", xml, flags=re.S)
    xml = re.sub(r"<w:p [^>]*>|<w:p>", "\n", xml)
    xml = re.sub(r"<w:tab/>", "\t", xml)
    xml = re.sub(r"<[^>]+>", "", xml)
    return html.unescape(xml).strip()


def extract_html(data: bytes) -> str:
    from bs4 import BeautifulSoup
    return BeautifulSoup(data, "html.parser").get_text("\n").strip()


def extract_doc_batch(items: list[tuple[str, bytes]]) -> dict[str, str]:
    """Convert legacy .doc payloads with one LibreOffice invocation."""
    out: dict[str, str] = {}
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        for name, data in items:
            (tdp / f"{name}.doc").write_bytes(data)
        subprocess.run(
            ["soffice", "--headless", "--convert-to", "txt:Text",
             "--outdir", td] + [str(tdp / f"{n}.doc") for n, _ in items],
            capture_output=True, timeout=600)
        for name, _ in items:
            txt = tdp / f"{name}.txt"
            out[name] = txt.read_text(errors="replace").strip() \
                if txt.exists() else ""
    return out


def sample(zip_path: str, n: int) -> None:
    rng = random.Random(42)
    with zipfile.ZipFile(zip_path) as zf:
        lobs = [i for i in zf.namelist()
                if i.startswith("BILL_ANALYSIS_TBL_") and i.endswith(".lob")]
        picks = rng.sample(lobs, min(n, len(lobs)))
        results = {}
        doc_batch = []
        for name in picks:
            data = zf.read(name)
            kind = detect(data)
            if kind == "doc":
                doc_batch.append((name.removesuffix(".lob"), data))
                results[name] = (kind, None)
            elif kind == "docx":
                results[name] = (kind, extract_docx(data))
            elif kind in ("html", "text"):
                results[name] = (kind, extract_html(data))
            else:
                results[name] = (kind, "")
        if doc_batch:
            converted = extract_doc_batch(doc_batch)
            for base, text in converted.items():
                results[base + ".lob"] = ("doc", text)

    kinds = {}
    ok = empty = 0
    for name, (kind, text) in results.items():
        kinds[kind] = kinds.get(kind, 0) + 1
        if text and len(text) > 200:
            ok += 1
        else:
            empty += 1
            print(f"  POOR {name} ({kind}): {len(text or '')} chars")
    print(f"{zip_path}: {len(results)} sampled, formats {kinds}, "
          f"{ok} good, {empty} poor")
    good = next((t for _, (k, t) in sorted(results.items())
                 if t and len(t) > 500), None)
    if good:
        print("--- sample extract ---")
        print("\n".join(good.splitlines()[:12]))


if __name__ == "__main__":
    sample(sys.argv[2], int(sys.argv[3]))

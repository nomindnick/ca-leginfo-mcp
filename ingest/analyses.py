"""Extract text from bill analysis lobs.

Dispatch on file magic, never on era (SPEC §6): the transition years are not
trustworthy and formats mix within a session. Observed formats: plain text
(1993), HTML (1995–2005), legacy binary .doc (2009–2013, via LibreOffice
headless in batches), .docx (2015+, via stdlib zipfile). No PDFs exist in
the corpus. The 2025–26 session is 100% .docx, so the nightly current-db
build needs no LibreOffice.
"""

from __future__ import annotations

import html
import io
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

Format = str  # "doc" | "docx" | "html" | "rtf" | "text"


def detect(data: bytes) -> Format:
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
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        xml = z.read("word/document.xml").decode("utf-8", errors="replace")
    # Field instruction text (MERGEFIELD codes etc.) is markup, not content.
    xml = re.sub(r"<w:instrText[^>]*>.*?</w:instrText>", "", xml, flags=re.DOTALL)
    xml = re.sub(r"<w:p [^>]*>|<w:p>", "\n", xml)
    xml = re.sub(r"<w:tab/>", "\t", xml)
    # Hard line breaks (<w:br/>, incl. attributed forms like page breaks,
    # and <w:cr/>) must become newlines, or adjacent runs jam together.
    xml = re.sub(r"<w:br\b[^>]*/>|<w:cr/>", "\n", xml)
    xml = re.sub(r"<[^>]+>", "", xml)
    return html.unescape(xml).strip()


def extract_html(data: bytes) -> str:
    from bs4 import BeautifulSoup

    text = BeautifulSoup(data, "html.parser").get_text("\n").strip()
    # 1990s-era analysis HTML is whitespace art: collapse trailing spaces
    # and runs of blank lines, keep paragraph breaks.
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text)


def extract_text(data: bytes) -> str:
    """1990s plain-text analyses predate UTF-8 — fall back to cp1252 (curly
    quotes, section signs) rather than mangling bytes to U+FFFD."""
    try:
        return data.decode("utf-8").strip()
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace").strip()


def soffice_available() -> bool:
    return shutil.which("soffice") is not None


def extract_doc_batch(items: list[tuple[str, bytes]],
                      timeout: int = 600,
                      profile_dir: str | None = None) -> dict[str, str]:
    """Convert legacy .doc payloads with one LibreOffice invocation.

    Returns {name: text}; a file LibreOffice could not convert maps to "".
    Batching matters: one soffice start-up amortized over ~30 files ran at
    30 files / 2.4 s in the spike.

    profile_dir: a distinct LibreOffice user-profile directory per
    concurrent caller — soffice instances sharing a profile refuse to run
    in parallel; with distinct profiles they parallelize cleanly.
    """
    out: dict[str, str] = {}
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        for name, data in items:
            (tdp / f"{name}.doc").write_bytes(data)
        cmd = ["soffice", "--headless"]
        if profile_dir:
            cmd.append(f"-env:UserInstallation=file://{profile_dir}")
        cmd += (["--convert-to", "txt:Text", "--outdir", td]
                + [str(tdp / f"{n}.doc") for n, _ in items])
        subprocess.run(cmd, capture_output=True, timeout=timeout, check=False)
        for name, _ in items:
            txt = tdp / f"{name}.txt"
            out[name] = (txt.read_text(errors="replace").strip()
                         if txt.exists() else "")
    return out


def extract_one(data: bytes) -> tuple[Format, str | None]:
    """Extract a single lob's text; .doc returns (\"doc\", None) — callers
    must collect those and run extract_doc_batch."""
    kind = detect(data)
    if kind == "docx":
        return kind, extract_docx(data)
    if kind == "html":
        return kind, extract_html(data)
    if kind == "text":
        return kind, extract_text(data)
    return kind, None  # doc (batch separately) or rtf (none observed yet)

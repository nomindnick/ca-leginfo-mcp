"""Parser for pubinfo .dat table files.

Format (per the load scripts in pubinfo_load.zip): MySQL LOAD DATA
conventions — fields terminated by tab, optionally enclosed by backticks,
lines terminated by newline, backslash as the escape character. A bare
(unenclosed) ``NULL`` or ``\\N`` is SQL NULL; the same characters inside a
backtick-enclosed field are literal text.
"""

from __future__ import annotations

Row = list[str | None]

# MySQL LOAD DATA control escapes: "\n" in the file is a linefeed in the
# data (embedded terminators MUST be escaped to survive the format), not
# the letter n. Any other escaped character maps to itself.
_ESCAPES = {"0": "\0", "b": "\b", "n": "\n", "r": "\r", "t": "\t",
            "Z": "\x1a"}


def parse_bytes(data: bytes) -> list[Row]:
    """Decode and parse a whole .dat file. pubinfo data is UTF-8 with
    occasional legacy bytes; latin-1 is the observed fallback."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("latin-1")
    return parse_text(text)


def parse_text(text: str) -> list[Row]:
    if "\\" not in text:
        return _parse_fast(text)
    return _parse_slow(text)


def _strip_field(raw: str) -> str | None:
    if len(raw) >= 2 and raw[0] == "`" and raw[-1] == "`":
        return raw[1:-1]
    if raw == "NULL":
        return None
    return raw


def _parse_fast(text: str) -> list[Row]:
    # No escape character anywhere in the file, so terminators are unambiguous.
    rows = []
    for line in text.split("\n"):
        if line:
            rows.append([_strip_field(f) for f in line.split("\t")])
    return rows


def _parse_slow(text: str) -> list[Row]:
    """Character state machine honoring backslash escapes and the rule that a
    closing backtick only closes the field when followed by a terminator."""
    rows: list[Row] = []
    fields: Row = []
    i, n = 0, len(text)
    while i < n:
        if text[i] == "`":  # enclosed field
            i += 1
            parts: list[str] = []
            while i < n:
                c = text[i]
                if c == "\\" and i + 1 < n:
                    nxt = text[i + 1]
                    parts.append(_ESCAPES.get(nxt, nxt))
                    i += 2
                    continue
                if c == "`" and (i + 1 >= n or text[i + 1] in "\t\n"):
                    i += 1
                    break
                parts.append(c)
                i += 1
            fields.append("".join(parts))
        else:  # bare field
            start = i
            parts = []
            while i < n and text[i] not in "\t\n":
                c = text[i]
                if c == "\\" and i + 1 < n:
                    nxt = text[i + 1]
                    parts.append(_ESCAPES.get(nxt, nxt))
                    i += 2
                    continue
                parts.append(c)
                i += 1
            raw = text[start:i]
            if raw in ("NULL", "\\N"):
                fields.append(None)
            else:
                fields.append("".join(parts))
        if i < n:
            if text[i] == "\t":
                i += 1
            elif text[i] == "\n":
                i += 1
                rows.append(fields)
                fields = []
    if fields:
        rows.append(fields)
    return rows

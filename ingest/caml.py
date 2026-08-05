"""Convert CAML XML (law sections, bill version titles) to plain text.

CAML is uniform across every pubinfo era back to 1989 — one parser serves
current data and archives alike.
"""

from __future__ import annotations

import html
import re

_TAG = re.compile(r"<[^>]+>")
# PI data attributes can contain raw '>' (e.g. markup captured inside
# deletion/insertion marks) — [^>]* would stop there and strand a '?>'
# fragment in the output. Non-greedy to the real terminator instead.
_PI = re.compile(r"<\?.*?\?>", re.DOTALL)
_DELETION = re.compile(r"<\?xm-deletion_mark.*?\?>", re.DOTALL)


def law_section_text(xml: str) -> str:
    """Flatten a <caml:Content> law section lob to readable plain text.

    Tables in statute XML lose their structure here — acceptable for
    verification text (documented tool limitation).
    """
    s = re.sub(r"</p\s*>", "\n", xml)
    s = re.sub(r'<span class="(?:En|Em)Space"\s*/>', " ", s)
    s = re.sub(r"<br\s*/?>", "\n", s)
    s = _TAG.sub("", s)
    s = html.unescape(s)
    s = re.sub(r"[ \t ]+", " ", s)
    s = re.sub(r" ?\n ?", "\n", s)
    return s.strip()


_TITLE_START = re.compile(r"<caml:Title[ >]")


def bill_text(bill_xml: str) -> str:
    """Flatten a bill-version lob to the readable text of THAT version:
    title + digest + bill content.

    The MeasureDoc header (ids, action dates, author records) precedes
    <caml:Title> in every era 1989->present — flattening starts there, or
    the metadata field values would jam into a garbage prefix. Deletion
    marks carry their removed text inside the PI's data attribute (not as
    element content), so stripping all PIs yields exactly the text as
    amended at this version — same rule extract_title relies on.
    """
    m = _TITLE_START.search(bill_xml)
    s = bill_xml[m.start():] if m else bill_xml
    s = _DELETION.sub("", s)
    s = _PI.sub("", s)
    return law_section_text(s)


def extract_title(bill_xml: str) -> str | None:
    """Pull the Legislative Counsel title from a bill version lob.

    Deletion marks carry their removed text inside the PI's data attribute,
    so stripping all PIs leaves exactly the current (post-amendment) title.
    """
    m = re.search(r"<caml:Title>(.*?)</caml:Title>", bill_xml, re.DOTALL)
    if not m:
        return None
    s = _DELETION.sub("", m.group(1))
    s = _PI.sub("", s)
    s = _TAG.sub("", s)
    s = html.unescape(s)
    return " ".join(s.split())

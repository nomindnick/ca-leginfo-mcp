"""Convert CAML XML (law sections, bill version titles) to plain text."""

import html
import re

_TAG = re.compile(r"<[^>]+>")
_PI = re.compile(r"<\?[^>]*\?>")
_DELETION = re.compile(r"<\?xm-deletion_mark[^>]*\?>")


def law_section_text(xml: str) -> str:
    """Flatten a <caml:Content> law section lob to readable plain text."""
    s = re.sub(r"</p\s*>", "\n", xml)
    s = re.sub(r'<span class="(?:En|Em)Space"\s*/>', " ", s)
    s = re.sub(r"<br\s*/?>", "\n", s)
    s = _TAG.sub("", s)
    s = html.unescape(s)
    s = re.sub(r"[ \t ]+", " ", s)
    s = re.sub(r" ?\n ?", "\n", s)
    return s.strip()


def extract_title(bill_xml: str) -> str | None:
    """Pull the Legislative Counsel title from a bill version lob.

    Deletion marks carry their removed text inside the PI's data attribute,
    so stripping all PIs leaves exactly the current (post-amendment) title.
    """
    m = re.search(r"<caml:Title>(.*?)</caml:Title>", bill_xml, re.S)
    if not m:
        return None
    s = _DELETION.sub("", m.group(1))
    s = _PI.sub("", s)
    s = _TAG.sub("", s)
    s = html.unescape(s)
    return " ".join(s.split())

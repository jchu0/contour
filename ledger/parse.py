"""Filing text extraction.

Turns EDGAR HTML into plain text and splits 10-K / 10-Q bodies into their
numbered Items, so the state extractor sees one coherent section at a time
rather than a megabyte of markup.
"""

from __future__ import annotations

import re
import warnings
from dataclasses import dataclass

from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

# Some filings are XHTML; the HTML parser handles them fine and the warning is noise.
warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

# Item bodies below this length are almost always table-of-contents entries,
# where every "Item N" line sits adjacent to the next one.
MIN_SECTION_CHARS = 400

# Filings mark headings with inline emphasis rather than heading tags, and that
# formatting is the only reliable signal of where one risk factor ends and the
# next begins. Flattening to text would discard it, so emphasis-only blocks are
# prefixed with this sentinel and downstream splitters key on it.
HEADING_MARK = "\ue000"

# A heading is a short, self-contained block. Longer emphasised runs are
# emphasis inside a paragraph, not a title.
_HEADING_RANGE = range(12, 320)

_BLOCK_TAGS = ("p", "div", "tr", "br", "li", "h1", "h2", "h3", "h4", "table")

_PART_RE = re.compile(r"^[\s\ue000]*PART\s+([IVX]+)\b", re.IGNORECASE)
_ITEM_RE = re.compile(
    r"^[\s\ue000]*ITEM\s+(\d{1,2}[A-Z]?)\s*[\.\-–—:\s]", re.IGNORECASE
)

_EMPHASIS_TOKENS = ("font-weight:700", "font-weight:bold", "font-style:italic")


def _is_emphasised(tag) -> bool:
    style = (tag.get("style") or "").replace(" ", "").lower()
    return any(token in style for token in _EMPHASIS_TOKENS)


def _mark_headings(soup) -> None:
    """Prefix blocks whose whole text is emphasised, in place."""
    for block in soup.find_all(["div", "p"]):
        if block.find(["div", "p"]):        # containers, not leaves
            continue
        text = block.get_text(" ", strip=True)
        if len(text) not in _HEADING_RANGE:
            continue
        emphasised = "".join(
            span.get_text(" ", strip=True)
            for span in block.find_all(["span", "b", "strong", "i", "em"])
            if span.name in ("b", "strong", "i", "em") or _is_emphasised(span)
        )
        if emphasised and len(emphasised) >= len(text) * 0.9:
            block.insert(0, HEADING_MARK)


@dataclass(frozen=True)
class Section:
    part: str | None   # "I" / "II" for 10-Q; None when the filing has no parts
    item: str          # "1A", "7", "2"
    title: str         # the header line as filed
    body: str

    @property
    def key(self) -> str:
        return f"{self.part}:{self.item}" if self.part else self.item


def html_to_text(html: str, *, mark_headings: bool = True) -> str:
    """Flatten filing HTML to text, preserving block boundaries as newlines.

    With `mark_headings`, emphasis-only blocks keep a leading sentinel so the
    document's own heading structure survives the flattening.
    """
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style"]):
        tag.decompose()
    if mark_headings:
        _mark_headings(soup)
    for name in _BLOCK_TAGS:
        for tag in soup.find_all(name):
            tag.append("\n")
    text = soup.get_text()
    text = text.replace("\xa0", " ").replace("​", "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return "\n".join(line.strip() for line in text.splitlines()).strip()


def split_items(text: str) -> list[Section]:
    """Split filing text into Item sections, discarding table-of-contents hits."""
    lines = text.splitlines()
    offsets, pos = [], 0
    for line in lines:
        offsets.append(pos)
        pos += len(line) + 1

    markers: list[tuple[int, str | None, str, str]] = []
    part: str | None = None
    for idx, line in enumerate(lines):
        part_match = _PART_RE.match(line)
        if part_match and len(line) < 40:
            part = part_match.group(1).upper()
            continue
        item_match = _ITEM_RE.match(line)
        if item_match:
            markers.append((offsets[idx], part, item_match.group(1).upper(), line.strip()))

    sections: list[Section] = []
    for i, (start, part_at, item, title) in enumerate(markers):
        end = markers[i + 1][0] if i + 1 < len(markers) else len(text)
        body = text[start:end].strip()
        if len(body) < MIN_SECTION_CHARS:
            continue
        sections.append(Section(part=part_at, item=item, title=title, body=body))
    return sections


def find_section(sections: list[Section], item: str, part: str | None = None) -> Section | None:
    """Longest matching section wins — filings often cross-reference an Item
    in passing, and the real body is the substantial one."""
    matches = [
        s for s in sections
        if s.item == item.upper() and (part is None or s.part == part.upper())
    ]
    return max(matches, key=lambda s: len(s.body)) if matches else None

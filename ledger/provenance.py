"""Source provenance and reliability classing.

Every fact the ledger reports carries the source it came from, and sources are
not equal. A claim is only ever described as *verified* when a primary
authoritative source supports it — a regulator, a court, a filing. Community
signal can corroborate; it can never promote a claim on its own.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from enum import Enum
from urllib.parse import quote


class SourceClass(Enum):
    """Reliability tiers, most authoritative first."""

    A_PRIMARY = ("A", "Primary authoritative", 100)   # SEC, courts, regulators, USPTO
    B_COMPANY = ("B", "Company primary", 90)          # IR site, earnings calls, company filings
    C_INDEPENDENT = ("C", "Independent reporting", 80)  # Reuters, Bloomberg, AP
    D_COMMERCIAL = ("D", "Structured commercial", 75)   # PitchBook, D&B, Capital IQ
    E_RELEASE = ("E", "Press release", 65)              # PR Newswire, Business Wire
    F_COMMUNITY = ("F", "Community signal", 40)         # Glassdoor, Reddit, review sites

    def __init__(self, letter: str, label: str, weight: int):
        self.letter = letter
        self.label = label
        self.weight = weight


@dataclass(frozen=True)
class Source:
    """One citation, carrying everything needed to check the claim by hand."""

    klass: SourceClass
    origin: str            # "SEC EDGAR", "NHTSA"
    reference: str         # accession number, recall campaign number
    document_date: date    # when the source itself was published or filed
    retrieved: date        # when we read it
    url: str
    passage: str | None = None   # exact supporting text, where the source is prose
    # Set when the company was matched to this source by a name a model
    # proposed rather than a person confirmed. The record may be the right
    # company's or another's, and nothing resting on it may read as verified.
    entity_proposed: bool = False
    # A scroll-to-text fragment ("#:~:text=…") or element anchor that lands the
    # reader on the sentence the finding was read from. Without it a citation
    # opens a document and leaves them to search it, where the same figure
    # often appears several times over.
    fragment: str = ""

    @property
    def href(self) -> str:
        """The citation link, pointed as precisely as the source allows."""
        if not self.url:
            return ""
        if not self.fragment or "#" in self.url:
            return self.url
        return f"{self.url}#{self.fragment}"

    @property
    def verified(self) -> bool:
        """Class A, and matched to this company by something better than a guess."""
        return self.klass is SourceClass.A_PRIMARY and not self.entity_proposed

    def cite(self) -> str:
        return (f"{self.origin} · {self.reference} · {self.document_date.isoformat()}"
                f" · Class {self.klass.letter}")


def is_verified(sources: tuple[Source, ...]) -> bool:
    """A claim is verified only when a Class-A primary source supports it,
    matched to the company by something firmer than a proposed name."""
    return any(s.verified for s in sources)


def confidence(sources: tuple[Source, ...]) -> int:
    """Weight of the strongest source, nudged up when others corroborate it.

    Deliberately blunt. It ranks findings for display; it is not a probability
    and must not be presented as one.
    """
    if not sources:
        return 0
    weights = sorted((s.klass.weight for s in sources), reverse=True)
    return min(99, weights[0] + sum(w // 20 for w in weights[1:]))


# -- pointing at the exact place ------------------------------------------

_WS = re.compile(r"\s+")


def _clean(text: str) -> str:
    return _WS.sub(" ", (text or "").replace(" ", " ")).strip()


def text_fragment(passage: str, *, words: int = 9) -> str:
    """A scroll-to-text fragment for the sentence a finding was read from.

    Browsers that support it (Chrome, Edge, Safari) scroll to the first match
    and highlight it. A short needle is the failure mode this exists to avoid:
    "Revenue" appears hundreds of times in a 10-K, so take a run of words long
    enough to be unique and cite a range rather than one phrase.
    """
    body = _clean(passage)
    if len(body) < 24:
        return ""
    parts = body.split(" ")
    head = " ".join(parts[:words])
    if len(parts) <= words * 2:
        return "%s%s" % (":~:text=", quote(head, safe=""))
    # start,end range — anchors both ends of a long passage, which survives
    # intervening markup that a single phrase would trip over.
    tail = " ".join(parts[-min(words, 6):])
    return ":~:text=%s,%s" % (quote(head, safe=""), quote(tail, safe=""))

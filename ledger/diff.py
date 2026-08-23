"""Year-over-year section diffing.

Deterministic first: blocks are paired by lexical overlap with no model in the
loop, so the same two filings always produce the same delta set. Judgment about
which deltas *matter* happens downstream, on top of this stable substrate.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

# Below this overlap two blocks are treated as unrelated rather than edited.
PAIR_THRESHOLD = 0.35
# Headings are short and stable, so a genuine match scores higher than bodies do.
HEADING_PAIR_THRESHOLD = 0.45
# Above this they are the same block with cosmetic edits only.
UNCHANGED_THRESHOLD = 0.97
# Blocks shorter than this are headers, page furniture, or stray table cells.
MIN_BLOCK_CHARS = 200
LEAD_CHARS = 220               # a risk-factor heading is a sentence, not a label


def _clip_words(text: str, limit: int) -> str:
    """Trim to `limit` without splitting the last word."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    space = cut.rfind(" ")
    if space > limit * 0.6:
        cut = cut[:space]
    return cut.rstrip(" ,;:-") + "\u2026"

# Opens lowercase, or with a word that cannot begin a risk factor: the
# continuation of one split across a page break. Deliberately NOT
# case-insensitive — under re.I the [a-z] class matches capitals too, which
# makes every heading a continuation and collapses the section to one block.
_CONTINUATION = re.compile(r"^(?:[a-z]|(?:And|Or|Which|That|But)\b)")


class Change(str, Enum):
    ADDED = "added"
    REMOVED = "removed"
    MODIFIED = "modified"
    UNCHANGED = "unchanged"


@dataclass(frozen=True)
class Block:
    index: int
    text: str
    heading: str | None = None

    @property
    def lead(self) -> str:
        """The block's own heading where the filing marked one, else its first sentence.

        Clipped on a word boundary. A hard character cut ended these mid-word
        ("...generally competitive, cycl"), and the lead is what an added risk
        factor is matched against the removed one it replaces — a broken last
        word costs exactly the comparison the lead exists for.
        """
        if self.heading:
            return _clip_words(self.heading, LEAD_CHARS)
        head = re.split(r"(?<=[.!?])\s", self.text.strip(), maxsplit=1)[0]
        return _clip_words(head, LEAD_CHARS)


@dataclass(frozen=True)
class Delta:
    change: Change
    similarity: float
    old: Block | None
    new: Block | None

    @property
    def lead(self) -> str:
        return (self.new or self.old).lead


def split_blocks(body: str) -> list[Block]:
    raw = re.split(r"\n\s*\n", body)
    blocks, buf = [], ""
    for chunk in raw:
        chunk = chunk.strip()
        if not chunk:
            continue
        buf = f"{buf}\n{chunk}".strip() if buf else chunk
        if len(buf) >= MIN_BLOCK_CHARS:
            blocks.append(buf)
            buf = ""
    if buf and blocks:
        blocks[-1] = f"{blocks[-1]}\n{buf}"
    elif buf:
        blocks.append(buf)
    return [Block(index=i, text=b) for i, b in enumerate(blocks)]


def _shingles(text: str, n: int = 3) -> set[str]:
    words = re.findall(r"[a-z0-9]+", text.lower())
    if len(words) < n:
        return {" ".join(words)}
    return {" ".join(words[i : i + n]) for i in range(len(words) - n + 1)}


def similarity(a: str, b: str) -> float:
    sa, sb = _shingles(a), _shingles(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def diff_blocks(old: list[Block], new: list[Block]) -> list[Delta]:
    """Greedy best-match pairing, highest-similarity pairs claimed first.

    Where both sides carry headings, pairing runs on the heading rather than the
    body. A heading is the risk factor's identity; the body is what changed.
    Pairing on body text conflates the two, so a heavily rewritten risk factor
    reads as one removal plus one addition instead of a rewrite — which is both
    wrong and less useful than saying how much of it was rewritten.
    """
    by_heading = all(b.heading for b in old) and all(b.heading for b in new)
    threshold = HEADING_PAIR_THRESHOLD if by_heading else PAIR_THRESHOLD

    def key(block: Block) -> str:
        return block.heading if by_heading else block.text

    candidates = []
    for nb in new:
        for ob in old:
            score = similarity(key(ob), key(nb))
            if score >= threshold:
                candidates.append((score, nb.index, ob.index))
    candidates.sort(reverse=True)

    paired_new: dict[int, tuple[int, float]] = {}
    paired_old: set[int] = set()
    for score, ni, oi in candidates:
        if ni in paired_new or oi in paired_old:
            continue
        paired_new[ni] = (oi, score)
        paired_old.add(oi)

    deltas: list[Delta] = []
    for nb in new:
        if nb.index not in paired_new:
            deltas.append(Delta(Change.ADDED, 0.0, None, nb))
            continue
        oi, _match_score = paired_new[nb.index]
        # Report body overlap, not the score that paired them — the useful
        # number is how much of the risk factor was rewritten.
        body_score = similarity(old[oi].text, nb.text)
        change = Change.UNCHANGED if body_score >= UNCHANGED_THRESHOLD else Change.MODIFIED
        deltas.append(Delta(change, body_score, old[oi], nb))
    for ob in old:
        if ob.index not in paired_old:
            deltas.append(Delta(Change.REMOVED, 0.0, ob, None))
    return deltas


def summarize(deltas: list[Delta]) -> dict[str, int]:
    counts = {c.value: 0 for c in Change}
    for d in deltas:
        counts[d.change.value] += 1
    return counts


def split_risk_factors(body: str) -> list[Block]:
    """Split a risk-factors section into one block per risk factor.

    Prefers the document's own heading structure, carried through flattening as
    a sentinel. Falls back to length-based blocking when a filing marks no
    headings — that produces boundaries that straddle risk factors, so the
    fallback is a degraded mode, not an equivalent one.
    """
    from ledger.parse import HEADING_MARK

    if HEADING_MARK not in body:
        return split_blocks(body)

    chunks = body.split(HEADING_MARK)

    # A running page header — the registrant's own name at the top of every
    # page — is emphasised exactly like a risk-factor heading, so it arrives
    # here as one. It gives itself away by repeating: a real risk factor is
    # stated once. Rivian's 10-K carried six of them, and each became an
    # "added risk factor" whose whole text was the company's name.
    seen: dict[str, int] = {}
    for chunk in chunks[1:]:
        first = next((line.strip() for line in chunk.splitlines() if line.strip()), "")
        if first:
            seen[first] = seen.get(first, 0) + 1
    furniture = {text for text, count in seen.items() if count > 1}

    blocks: list[Block] = []
    strict: list[Block] = []
    for chunk in chunks[1:]:
        lines = [line for line in chunk.splitlines() if line.strip()]
        if not lines:
            continue
        heading = lines[0].strip()
        # The Item header itself is emphasised; its preamble is section framing,
        # not a risk factor, and diffing it just adds noise every year.
        if re.match(r"^Item\s+\d", heading, re.IGNORECASE):
            continue
        if heading in furniture:
            continue
        # A heading that opens mid-sentence is a risk factor broken across a
        # page, not a new one. Its text belongs to the block above it.
        if _CONTINUATION.match(heading) and blocks:
            previous = blocks[-1]
            blocks[-1] = Block(index=previous.index,
                               text=f"{previous.text}\n{chunk.strip()}",
                               heading=previous.heading)
            if strict and strict[-1].index == previous.index:
                strict[-1] = blocks[-1]
            continue
        rest = "\n".join(lines[1:]).strip()
        # Category labels ("Business Risks") carry no prose of their own; the
        # next heading follows immediately. They are section dividers, not risks.
        if len(rest) < MIN_BLOCK_CHARS:
            continue
        block = Block(index=len(blocks), text=f"{heading}\n{rest}", heading=heading)
        blocks.append(block)
        # A risk factor is written as a sentence; a section divider ("Risks
        # Related to Supply Chain") is a noun phrase. Some filings open Item 1A
        # with a summary of dividers carrying enough prose to look like risks,
        # and a year where that summary is absent then pairs against nothing.
        if heading.rstrip().endswith((".", "?", "!")):
            strict.append(block)
    if len(strict) >= 3:
        return [Block(index=i, text=b.text, heading=b.heading) for i, b in enumerate(strict)]
    return blocks or split_blocks(body)

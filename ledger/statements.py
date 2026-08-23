"""Financial statement extraction from EDGAR rendered reports.

The XBRL companyfacts API omits company extension tags, so lines like Tesla's
automotive regulatory credits are invisible there. The rendered R-files in each
filing carry every line as filed, custom tags included, so they are the source
of truth for statement-level work.
"""

from __future__ import annotations

import re
import warnings
from dataclasses import dataclass, field

from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

_SCALE = {"thousands": 1_000, "millions": 1_000_000, "billions": 1_000_000_000}
_DATE_RE = re.compile(r"[A-Z][a-z]{2}\.?\s+\d{1,2},\s+\d{4}")
# Titles read "shares in Thousands, $ in Millions" — anchor on the currency unit,
# never the share unit, or every figure comes out off by three orders of magnitude.
_MONEY_SCALE_RE = re.compile(r"\$\s*in\s+(thousands|millions|billions)", re.IGNORECASE)


@dataclass
class Statement:
    name: str
    periods: list[str]
    scale: int
    url: str = ""
    rows: dict[str, list[float | None]] = field(default_factory=dict)
    # The rendered R-file this came from. A 10-K is megabytes and repeats a
    # figure in half a dozen places; the R-file is the one table it was read
    # from, so a citation should point there and not at the whole filing.
    url: str = ""

    def value(self, *label_fragments: str, period_index: int = 0) -> float | None:
        """Look up a line by label, trying each fragment in order.

        Issuers name the same concept differently — Apple's "Operating income"
        is Tesla's "Income from operations" — so callers pass every spelling.
        Exact matches win over substring matches, since "Net income" would
        otherwise collide with "Net income attributable to noncontrolling
        interests".
        """
        for fragment in label_fragments:
            needle = fragment.lower().strip()
            for label, values in self.rows.items():
                if label.lower().strip().rstrip(":") == needle and period_index < len(values):
                    v = values[period_index]
                    return None if v is None else v * self.scale
        for fragment in label_fragments:
            needle = fragment.lower().strip()
            for label, values in self.rows.items():
                if needle in label.lower() and period_index < len(values):
                    v = values[period_index]
                    return None if v is None else v * self.scale
        return None


def _to_number(cell: str) -> float | None:
    text = cell.replace("$", "").replace(",", "").replace("%", "").strip()
    if not text or text in {"—", "–", "-"}:
        return None
    negative = text.startswith("(") and text.endswith(")")
    text = text.strip("()")
    try:
        value = float(text)
    except ValueError:
        return None
    return -value if negative else value


def parse_report(html: str) -> Statement:
    soup = BeautifulSoup(html, "lxml")
    rows = soup.find_all("tr")
    if not rows:
        raise ValueError("no table rows in report")

    title = rows[0].get_text(" ", strip=True)
    money = _MONEY_SCALE_RE.search(title)
    scale = _SCALE[money.group(1).lower()] if money else 1
    name = title.split(" - ")[0].strip()

    periods: list[str] = []
    data: dict[str, list[float | None]] = {}
    for tr in rows:
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])]
        cells = [c for c in cells if c]
        if not cells:
            continue
        found = [c for c in cells if _DATE_RE.search(c)]
        if found and not periods:
            periods = found
            continue
        label, values = cells[0], [_to_number(c) for c in cells[1:]]
        if any(v is not None for v in values):
            data.setdefault(label, values)
    return Statement(name=name, periods=periods, scale=scale, rows=data)


def report_index(client, filing) -> list[tuple[str, str]]:
    base = f"https://www.sec.gov/Archives/edgar/data/{filing.cik}/{filing.accession.replace('-', '')}"
    soup = BeautifulSoup(client.get(f"{base}/FilingSummary.xml"), "xml")
    out = []
    for report in soup.find_all("Report"):
        short = report.find("ShortName")
        fname = report.find("HtmlFileName") or report.find("XmlFileName")
        if short and fname:
            out.append((short.text.strip(), f"{base}/{fname.text.strip()}"))
    return out


def find_report(client, filing, *keywords: str) -> Statement | None:
    """Fetch the first report whose title contains all keywords."""
    for name, url in report_index(client, filing):
        lowered = name.lower()
        if all(k.lower() in lowered for k in keywords):
            statement = parse_report(client.get(url))
            statement.name = name
            statement.url = url
            return statement
    return None


# -- member-labeled breakdown tables ---------------------------------------

# XBRL structural markers that render as rows but carry no data.
_MARKER_TOKENS = ("[line items]", "[abstract]", "[member]", "[axis]", "[domain]",
                  "[table]", "[roll forward]")


@dataclass(frozen=True)
class Component:
    member: str                  # "Automotive regulatory credits"
    concept: str                 # "Revenues"
    values: tuple[float | None, ...]


@dataclass
class Breakdown:
    """A note table where each figure is scoped to an axis member.

    These render as a member label, then an XBRL marker row, then the values —
    so the member owning a value is the last label seen above it, not anything
    on the row itself.
    """

    name: str
    periods: list[str]
    scale: int
    total: tuple[float | None, ...]
    # The rendered R-file this table came from. A 10-K repeats a figure in
    # several places; the R-file is the one table it was read from.
    url: str = ""
    components: list[Component] = field(default_factory=list)

    def component(self, fragment: str) -> Component | None:
        needle = fragment.lower()
        return next((c for c in self.components if needle in c.member.lower()), None)

    def leaves(self, period: int = 0) -> list[Component]:
        """Components that do not decompose further.

        Note tables interleave subtotals with the lines that make them up, so
        summing every component double-counts. A component is treated as a
        subtotal when it equals the sum of a contiguous run of the components
        that follow it.
        """
        out: list[Component] = []
        for i, candidate in enumerate(self.components):
            target = candidate.values[period] if period < len(candidate.values) else None
            if target is None:
                continue
            running, is_subtotal = 0.0, False
            for follower in self.components[i + 1:]:
                value = follower.values[period] if period < len(follower.values) else None
                if value is None:
                    break
                running += value
                if abs(running - target) < 0.51:
                    is_subtotal = True
                    break
                if running > target:
                    break
            if not is_subtotal:
                out.append(candidate)
        # A member naming two axes at once is a matrix cell, not a component.
        # Excluding these in scoring but not here let Boeing chart 15 bars the
        # scorer had already rejected.
        return [c for c in out if "|" not in c.member]


def _is_marker(label: str) -> bool:
    lowered = label.lower()
    return any(token in lowered for token in _MARKER_TOKENS)


def parse_member_report(html: str) -> Breakdown:
    soup = BeautifulSoup(html, "lxml")
    rows = soup.find_all("tr")
    if not rows:
        raise ValueError("no table rows in report")

    title = rows[0].get_text(" ", strip=True)
    money = _MONEY_SCALE_RE.search(title)
    scale = _SCALE[money.group(1).lower()] if money else 1

    periods: list[str] = []
    total: tuple[float | None, ...] = ()
    components: list[Component] = []
    pending_member: str | None = None

    # rows[0] is the report title; without skipping it the title becomes the
    # first "member" and claims the grand-total row.
    for tr in rows[1:]:
        cells = tr.find_all(["th", "td"])
        texts = [c.get_text(" ", strip=True) for c in cells]
        if not texts or not texts[0]:
            continue

        dates = [t for t in texts if _DATE_RE.search(t)]
        if dates and not periods:
            periods = dates
            continue

        label, rest = texts[0], texts[1:]
        if _is_marker(label):
            continue

        values = tuple(_to_number(t) for t in rest)
        if all(v is None for v in values):
            # A label with no figures on its row announces the member that the
            # next data row belongs to.
            pending_member = label
            continue

        if pending_member is None:
            if not total:
                total = values
            continue

        components.append(Component(member=pending_member, concept=label, values=values))
        pending_member = None

    return Breakdown(
        name=title.split(" - ")[0].strip(),
        periods=periods,
        scale=scale,
        total=total,
        components=components,
    )


def find_member_report(client, filing, *keywords: str) -> Breakdown | None:
    for name, url in report_index(client, filing):
        lowered = name.lower()
        if all(k.lower() in lowered for k in keywords):
            breakdown = parse_member_report(client.get(url))
            breakdown.name = name
            breakdown.url = url
            return breakdown
    return None


# Issuers file the revenue breakdown under wildly different note titles, and a
# filing usually carries several candidates — a narrative, a geography matrix, a
# segment table. Matching the first title that looks right picks a bad one about
# half the time, so candidates are parsed and scored by what they actually yield.
_BREAKDOWN_KEYWORDS = (
    "disaggregat", "revenue by", "net sales", "net operating revenue",
    "revenue and", "revenues by", "by segment", "by market", "by item category",
    "segment information", "segment financial", "operating segments",
    "reportable segment", "segment reporting",
    # Lululemon files its breakdown as plain "Net Revenue (Details)", which
    # matched none of the phrases above.
    "net revenue", "revenue",
)

# Above this, the table is a segment x geography matrix rather than a breakdown.
_MAX_USEFUL_LEAVES = 20
# Leaves summing to more than this multiple of the reported total mean the table
# stacks several axes — Lululemon files channel, category and geography in one
# note, each summing to the same revenue. Attributing a change across a mix of
# them would add a channel figure to a geography figure.
_STACKED_AXIS_RATIO = 1.5
_MAX_CANDIDATES = 10

# A report title names its note first, then the specific table: "Goodwill … -
# Goodwill by Business Segment (Details)". Only the note name says what the
# table is *about*, so the exclusion applies there. Matching the whole title
# instead rejects Apple's own "Revenue - Disaggregated Net Sales and Portion of
# Net Sales That Was Previously Deferred" over the word "deferred".
_NOT_REVENUE_SUBJECT = (
    "goodwill", "long-lived", "intangible", "impairment", "property", "plant",
    "equipment", "compensation", "pension", "inventor", "receivable",
    "debt", "lease", "tax", "allowance", "obligation",
)


def _subject(title: str) -> str:
    """The note name — everything before the first dash separator."""
    for sep in (" - ", " – ", " — ", ": "):
        if sep in title:
            return title.split(sep)[0].lower()
    return title.lower()


def stacks_axes(breakdown: Breakdown, period: int = 0) -> bool:
    leaves = breakdown.leaves(period)
    values = [c.values[period] for c in leaves if period < len(c.values) and c.values[period]]
    total = breakdown.total[period] if period < len(breakdown.total) else None
    if not values or not total:
        return False
    return sum(values) > abs(total) * _STACKED_AXIS_RATIO


def _breakdown_score(breakdown: Breakdown) -> int:
    if len(breakdown.periods) < 2:
        return 0
    leaves = breakdown.leaves()
    if len(leaves) < 2:
        return 0
    # Every leaf needs a comparable prior period or nothing can be attributed.
    usable = [c for c in leaves if len(c.values) > 1 and c.values[0] is not None and c.values[1] is not None]
    # A member naming two axes at once ("Japan | Operating segments") is a cell
    # in a matrix, not a component of revenue. Attributing a change to one is
    # meaningless and reads as a parsing artefact, so those do not count.
    usable = [c for c in usable if "|" not in c.member]
    # Members must actually name different things. One NVIDIA note repeats
    # "Revenues and Long-Lived Assets" for every row — parseable, chartable, and
    # meaningless, which is worse than not finding a breakdown at all.
    if len({c.member for c in usable}) < len(usable):
        usable = [c for c in usable if len({x.member for x in usable}) == len(usable)]
    distinct = {c.member for c in usable}
    if len(distinct) < 2 or len(distinct) < len(usable):
        return 0
    if stacks_axes(breakdown):
        return 0
    return len(usable) if len(usable) <= _MAX_USEFUL_LEAVES else _MAX_USEFUL_LEAVES * 2 - len(usable)


def find_revenue_breakdown(client, filing) -> tuple[Breakdown | None, str | None]:
    """(breakdown, reason it was declined). The candidate note that yields the
    most attributable components, or nothing and why."""
    candidates = [
        (name, url) for name, url in report_index(client, filing)
        # Issuers title these "(Details)" or "(Detail)" — Lululemon uses the
        # singular, and requiring the plural skipped its segment schedule.
        if "(detail" in name.lower()
        and any(k in name.lower() for k in _BREAKDOWN_KEYWORDS)
        and not any(k in _subject(name) for k in _NOT_REVENUE_SUBJECT)
    ]
    best: Breakdown | None = None
    best_score = 0
    stacked: str | None = None
    for name, url in candidates[:_MAX_CANDIDATES]:
        try:
            breakdown = parse_member_report(client.get(url))
        except Exception:
            continue
        breakdown.name = name
        breakdown.url = url
        if stacks_axes(breakdown) and len(breakdown.leaves()) >= 3:
            stacked = name
        score = _breakdown_score(breakdown)
        if score > best_score:
            best, best_score = breakdown, score
    if best:
        return best, None
    if stacked:
        return None, (
            f'"{stacked}" stacks several axes in one table — channel, category and '
            "geography each summing to the same revenue — so no single decomposition "
            "is available without adding unlike figures together"
        )
    if not candidates:
        return None, "this filing has no revenue breakdown note"
    return None, "no revenue breakdown note here resolves to comparable components"


# -- reported history from XBRL -------------------------------------------

_QUARTER_DAYS = range(80, 100)

_HISTORY_CONCEPTS = {
    "revenue": (
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "Revenues",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        # Banks report revenue net of interest expense and never touch the
        # contract-revenue tags; without this JPMorgan's series ends in 2014.
        "RevenuesNetOfInterestExpense",
        "InterestAndDividendIncomeOperating",
    ),
    "eps": ("EarningsPerShareDiluted",),
    "operating_income": ("OperatingIncomeLoss",),
    "net_income": ("NetIncomeLoss",),
}


def quarterly_history(facts: dict) -> dict[str, list[tuple[str, float]]]:
    """Reported quarterly figures per metric, oldest first, labelled by period end.

    Periods are identified by the fact's own start/end dates, never by `fy`/`fp`
    — those describe the filing a fact appeared in, so a 10-Q's prior-year
    comparatives carry the current filing's fiscal tags and reading them as
    current quarters silently shifts the whole series by a year.

    Restatements put the same period in more than once; the most recently filed
    value wins.
    """
    from datetime import date as _date

    us_gaap = facts.get("facts", {}).get("us-gaap", {})
    out: dict[str, list[tuple[str, float]]] = {}

    for metric, concepts in _HISTORY_CONCEPTS.items():
        candidates: list[dict[tuple[str, str], tuple[float, str]]] = []
        latest: dict[tuple[str, str], tuple[float, str]] = {}
        for concept in concepts:
            entry = us_gaap.get(concept)
            if not entry:
                continue
            for unit_rows in entry.get("units", {}).values():
                for row in unit_rows:
                    start, end = row.get("start"), row.get("end")
                    if not start or not end:
                        continue
                    span = (_date.fromisoformat(end) - _date.fromisoformat(start)).days
                    if span not in _QUARTER_DAYS:
                        continue
                    key = (start, end)
                    filed = row.get("filed", "")
                    if key not in latest or filed > latest[key][1]:
                        latest[key] = (float(row["val"]), filed)
            if latest:
                candidates.append(latest)
                latest = {}
        if candidates:
            # Issuers migrate between tags. Taking the first concept that has any
            # data leaves you on an abandoned one — NVIDIA's series ended in 2020
            # that way, so a 2026 record claim was checked against 2018.
            best = max(candidates, key=lambda rows: max(end for _, end in rows))
            out[metric] = [
                (end, value)
                for (_, end), (value, _) in sorted(best.items(), key=lambda kv: kv[0][1])
            ]
    return out


def same_quarter(label: str, other: str, tolerance_days: int = 20) -> bool:
    """True when two period-end dates fall in the same fiscal quarter of different years."""
    from datetime import date as _date

    a, b = _date.fromisoformat(label), _date.fromisoformat(other)
    shifted = a.replace(year=b.year) if (a.month, a.day) != (2, 29) else a.replace(year=b.year, day=28)
    return abs((shifted - b).days) <= tolerance_days


def quarterly_observations(facts: dict) -> list[tuple[str, str, float, str, str]]:
    """Every reported version of every quarterly figure, as filed.

    `quarterly_history` collapses restatements by keeping the newest value —
    correct for reading the current number, useless for noticing that it moved.
    This keeps all versions so the store can hold them side by side.

    Returns (metric, period_end, value, filed, form).
    """
    from datetime import date as _date

    us_gaap = facts.get("facts", {}).get("us-gaap", {})
    out: list[tuple[str, str, float, str, str]] = []
    seen: set[tuple[str, str, str]] = set()

    for metric, concepts in _HISTORY_CONCEPTS.items():
        for concept in concepts:
            entry = us_gaap.get(concept)
            if not entry:
                continue
            for unit_rows in entry.get("units", {}).values():
                for row in unit_rows:
                    start, end, filed = row.get("start"), row.get("end"), row.get("filed")
                    if not start or not end or not filed:
                        continue
                    span = (_date.fromisoformat(end) - _date.fromisoformat(start)).days
                    if span not in _QUARTER_DAYS:
                        continue
                    key = (metric, end, filed)
                    if key in seen:
                        continue
                    seen.add(key)
                    out.append((metric, end, float(row["val"]), filed, row.get("form", "")))
            if seen:
                break
    return out

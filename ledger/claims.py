"""Claim extraction and substantiation.

Management statements in earnings releases are almost never false. They are
selective — a record that holds only within one fiscal quarter, a figure that is
accurate before a qualifier the company itself discloses two lines later. So the
vocabulary here is deliberately not true/false: a claim is SUPPORTED,
QUALIFIED, UNSUPPORTED, or NOT_TESTABLE, always against a named figure.

Extraction is pattern-based and substantiation is arithmetic. No model decides
whether a claim holds.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

_QUOTE = r"[“\"]([^”\"]{40,700})[”\"]"
_SPEAKER = r"\s*,?\s*said\s+([A-Z][a-zA-Z.\-]+(?:\s+[A-Z][a-zA-Z.\-]+){0,3})\s*,\s*([^.“\"]{3,60})"

_SUPERLATIVES = (
    "record", "all-time high", "highest ever", "best ever", "strongest",
    "best quarter", "second-best", "first time",
)

# Company-disclosed hedges that qualify an otherwise accurate headline.
_QUALIFIERS = (
    "including a favorable impact", "excluding", "adjusted", "non-gaap",
    "on a trailing twelve-month basis", "pro forma", "constant currency",
    "one-time", "benefited from",
)

_QUARTER_WORDS = (
    "march quarter", "june quarter", "september quarter", "december quarter",
    "first quarter", "second quarter", "third quarter", "fourth quarter",
    "first-quarter", "second-quarter", "third-quarter", "fourth-quarter",
    "q1 ", "q2 ", "q3 ", "q4 ",
)

# Glossary and safe-harbour text reads like a claim but asserts nothing about
# performance. Dropping it here keeps the claim list to statements worth testing.
_BOILERPLATE = (
    "is equal to", "is defined as", "is operating cash flow less",
    "discussion and analysis", "refer to the reconciliation",
    "forward-looking statements", "divided by",
)

_METRICS = {
    "revenue": ("revenue", "net sales", "total revenues"),
    "eps": ("earnings per share", "eps", "diluted earnings"),
    "operating_income": ("operating income",),
    "net_income": ("net income",),
    "gross_margin": ("gross margin",),
    "free_cash_flow": ("free cash flow",),
    "deliveries": ("deliveries", "vehicle deliveries"),
}

# Per-share figures are stored as filed and are NOT split-adjusted, so a record
# test across a split compares incomparable numbers. Apple's 2012 EPS of $9.32
# predates a 7:1 and a 4:1 split; testing today's EPS against it would call a
# true statement false.
# A record claim's own figure may round; beyond this it is a different number.
_RECORD_FIGURE_TOLERANCE = 0.02

_SPLIT_SENSITIVE = ("eps",)

# Forward-looking guidance is not a report of what happened; there is nothing
# filed to check it against.
_GUIDANCE = ("expected to be", "we expect", "outlook", "guidance", "forecast",
             "anticipate", "plus or minus")

# A figure introduced by a segment or product name describes that segment, not
# the company. "Commercial Airplanes second quarter revenue of $11.8 billion" is
# true; checking it against Boeing's $24.6B total calls a true statement false.
_SCOPED = (
    "segment", "data center", "commercial airplanes", "defense", "global services",
    "gaming", "networking", "automotive", "energy", "services and other",
    "compute", "professional visualization", "edge computing", "sub-markets",
    "iphone", "mac", "ipad", "wearables", "products", "membership", "advertising",
)

# Claims measured over a window the quarterly series cannot express.
_WINDOWED = ("trailing twelve-month", "trailing twelve month", "ttm", "annualized", "last twelve months")

# Releases footnote their figures inline — "$0.5B1" is half a billion with a
# marker, not fifty cents. \b fails there because B and 1 are both word
# characters, so the unit went unmatched and the value came out 1000x small.
_MONEY = re.compile(
    r"\$\s?([\d,]+(?:\.\d+)?)\s*(billion|million|B(?![a-zA-Z])|M(?![a-zA-Z]))?",
    re.IGNORECASE,
)
_PERCENT = re.compile(r"(up|down|grew|fell|increased|decreased)\s+([\d.]+)\s*(?:percent|%)", re.IGNORECASE)


class Kind(str, Enum):
    ATTRIBUTED = "attributed"      # a named executive said it
    HEADLINE = "headline"          # company voice, numeric
    SUPERLATIVE = "superlative"    # record / best / highest


class Scope(str, Enum):
    ALL_TIME = "all-time"
    SEASONAL = "same-quarter"      # "June quarter record" — a weaker claim
    NONE = "none"


class Verdict(str, Enum):
    SUPPORTED = "supported"
    QUALIFIED = "qualified"        # accurate as stated, but the company hedges it
    UNSUPPORTED = "unsupported"
    NOT_TESTABLE = "not testable"


@dataclass(frozen=True)
class Claim:
    kind: Kind
    text: str
    speaker: str | None = None
    speaker_title: str | None = None
    metrics: tuple[str, ...] = ()
    superlatives: tuple[str, ...] = ()
    qualifiers: tuple[str, ...] = ()
    scope: Scope = Scope.NONE

    @property
    def testable(self) -> bool:
        return bool(self.metrics) and (bool(self.superlatives) or bool(_MONEY.search(self.text)))


@dataclass(frozen=True)
class Substantiation:
    claim: Claim
    verdict: Verdict
    reason: str
    figures: dict[str, str] = field(default_factory=dict)


def _metrics_in(text: str) -> tuple[str, ...]:
    lowered = text.lower()
    return tuple(name for name, words in _METRICS.items() if any(w in lowered for w in words))


def _found(text: str, needles: tuple[str, ...]) -> tuple[str, ...]:
    lowered = text.lower()
    return tuple(n for n in needles if n in lowered)


# Look back this far from the metric word for a qualifier.
_SCOPE_LOOKBACK = 6


def _scoping_term(text: str) -> str | None:
    """A segment name only scopes a figure when it comes *before* the metric.

    "Commercial Airplanes second quarter revenue of $11.8 billion" is scoped.
    "our strongest June quarter ever, with revenue growth across iPhone, Mac and
    Services" is not — those names follow the metric and merely illustrate it.
    Matching anywhere in the sentence silences the company-level claims that
    matter most.
    """
    lowered = text.lower()
    words = lowered.split()
    for name, spellings in _METRICS.items():
        for word in spellings:
            index = lowered.find(word)
            if index < 0:
                continue
            before = " ".join(lowered[:index].split()[-_SCOPE_LOOKBACK:])
            for term in _SCOPED:
                if term in before:
                    return term
    return None


def _scope(text: str) -> Scope:
    lowered = text.lower()
    if any(q in lowered for q in _QUARTER_WORDS):
        return Scope.SEASONAL
    if any(w in lowered for w in ("all-time", "ever", "first time")):
        return Scope.ALL_TIME
    return Scope.NONE


# Shareholder decks flatten to text with no sentence boundaries: a slide heading
# and its stat block run straight into the prose beneath. "Profitability $0.4B
# GAAP operating income $1.1B GAAP net income $1.2B non-GAAP net income1 Q2 was a
# strong quarter…" is one "sentence" to a splitter, and quoting it verbatim under
# a pointing finger is the worst artefact a product about faithful citation can
# produce.
_SENTENCE_OPENERS = re.compile(
    r"\b(We |Our |This |These |The Company |Tesla |Q[1-4] (?:was|were|marked)|"
    r"In Q[1-4]|During )"
)
_FIGURE = re.compile(r"\$\s?[\d,.]+")
# Two or more Title-Case words in a row with no lowercase between them is a
# heading or a column label, not prose.
_TITLE_RUN = re.compile(r"(?:\b[A-Z][a-zA-Z]*\s+){3,}")
# A lead made only of Title-Case words is a slide heading, however short —
# "Automotive" in front of "Our overall Q2 record deliveries…".
_ALL_TITLE = re.compile(r"^(?:[A-Z][a-zA-Z]*\s+)+$")
# "$4.7B" contains a period. Counting every "." as a sentence ending made a pure
# stat block look like well-punctuated prose.
_SENTENCE_END = re.compile(r"[.!?](?:\s|$)")
# A Title-Case word sitting directly after a lowercase word, with no punctuation
# between, is a slide heading butted onto the tail of the previous sentence:
# "…inclusive of mix impact Profitability Our quarterly operating income…".
_HEADING_JOIN = re.compile(r"[a-z]\s+[A-Z][a-zA-Z]*\s*$")


def _trim_lead(text: str) -> str:
    """Cut a leading heading/stat run back to where the sentence actually starts."""
    match = _SENTENCE_OPENERS.search(text)
    if not match or match.start() == 0:
        return text
    lead = text[: match.start()]
    # Only trim when what precedes really is deck furniture.
    if (_FIGURE.search(lead) or _TITLE_RUN.search(lead + " ")
            or _ALL_TITLE.match(lead) or _HEADING_JOIN.search(lead)):
        return text[match.start():].strip()
    return text


# Bullet glyphs survive the flattening and lead the quote.
_BULLETS = "•◦▪–—-*· \t"


def _drop_leading_heading(text: str) -> str:
    """Strip a leading run of Title-Case section labels.

    "Revenue Total quarterly revenue increased 26%…" — "Revenue" is the slide
    heading. A token carrying punctuation is left alone, so "Today, Apple is
    proud…" keeps its opening word.
    """
    # Words that begin a real sentence and merely happen to be capitalised.
    # Without this, "The Company posted quarterly revenue…" loses its "The".
    keep = {"the", "a", "an", "this", "these", "our", "we", "in", "on", "at",
            "during", "for", "its", "their", "his", "her", "today", "first",
            "second", "third", "fourth"}
    words = text.split()
    while len(words) > 3:
        first, second = words[0], words[1]
        if first.lower().strip(".,;:") in keep:
            break
        if not first[:1].isupper() or not first[-1:].isalpha():
            break
        if not second[:1].isupper():
            break
        words = words[1:]
    return " ".join(words)


def _drop_repeated_heading(text: str) -> str:
    """A slide heading whose last word is also the sentence's first word.

    "Automotive Services Services and Other gross profit grew…" — the heading is
    "Automotive Services" and the sentence begins "Services and Other".
    """
    words = text.split()
    for i in range(1, min(len(words), 5)):
        if words[i - 1] == words[i] and words[i][:1].isupper():
            return " ".join(words[i:])
    return text


def _is_stat_block(text: str) -> bool:
    """A run of figures and labels with no sentence in it.

    Better to drop a claim than to quote a table. Nothing downstream can repair
    a quote that was never a sentence.
    """
    figures = len(_FIGURE.findall(text))
    if figures >= 3 and len(_SENTENCE_END.findall(text)) <= 1:
        return True
    words = len(text.split())
    return bool(words and figures / words > 0.12)


def _is_boilerplate(text: str) -> bool:
    lowered = text.lower()
    return any(token in lowered for token in _BOILERPLATE)


def _build(kind: Kind, text: str, **extra) -> Claim:
    text = re.sub(r"\s+", " ", text).strip().lstrip(_BULLETS).strip()
    text = _trim_lead(_drop_leading_heading(_drop_repeated_heading(text)))
    return Claim(
        kind=kind,
        text=text,
        metrics=_metrics_in(text),
        superlatives=_found(text, _SUPERLATIVES),
        qualifiers=_found(text, _QUALIFIERS),
        scope=_scope(text),
        **extra,
    )


def extract_claims(text: str, *, limit: int = 40) -> list[Claim]:
    """Pull attributed quotes, numeric headlines and superlative assertions."""
    claims: list[Claim] = []
    seen: set[str] = set()

    def add(claim: Claim) -> None:
        key = claim.text.strip("“”\"' ")[:120].lower()
        if key not in seen:
            seen.add(key)
            claims.append(claim)

    for match in re.finditer(_QUOTE, text):
        quote = match.group(1)
        if _is_boilerplate(quote) or _is_stat_block(quote):
            continue
        tail = text[match.end(): match.end() + 160]
        speaker_match = re.match(_SPEAKER, tail)
        add(_build(
            Kind.ATTRIBUTED, quote,
            speaker=speaker_match.group(1) if speaker_match else None,
            speaker_title=speaker_match.group(2).strip() if speaker_match else None,
        ))

    for sentence in re.split(r"(?<=[.!?])\s+|\n", text):
        sentence = sentence.strip()
        if not (25 < len(sentence) < 400) or sentence in seen:
            continue
        if _is_boilerplate(sentence) or _is_stat_block(_trim_lead(sentence)):
            continue
        metrics = _metrics_in(sentence)
        if not metrics:
            continue
        if _found(sentence, _SUPERLATIVES):
            add(_build(Kind.SUPERLATIVE, sentence))
        elif _MONEY.search(sentence) or _PERCENT.search(sentence):
            add(_build(Kind.HEADLINE, sentence))

    return claims[:limit]


_METRIC_LABELS = {
    "eps": "earnings per share",
    "operating_income": "operating income",
    "net_income": "net income",
    "gross_margin": "gross margin",
    "free_cash_flow": "free cash flow",
}


def _metric_label(metric: str) -> str:
    """Explanations are read by people; `operating_income` is a variable name."""
    return _METRIC_LABELS.get(metric, metric.replace("_", " "))


def _fmt(value: float) -> str:
    """Readable from the back of a room: $109.42B, not 109,417,000,000.00."""
    for scale, suffix in ((1e9, "B"), (1e6, "M")):
        if abs(value) >= scale:
            return f"${value / scale:,.2f}{suffix}"
    return f"{value:,.2f}"


# A figure belongs to a metric only if it sits close to it. Tesla's "revenue was
# impacted by … positive FX impact of $0.5B" mentions revenue and states a
# figure, but the figure is the FX impact. Binding them called a true sentence
# unsupported.
_FIGURE_PROXIMITY = 8


def _figure_near_metric(text: str, metrics: tuple[str, ...]) -> bool:
    lowered = text.lower()
    match = _MONEY.search(text)
    if not match:
        return False
    before = lowered[: match.start()].split()
    after = lowered[match.end():].split()
    window = " ".join(before[-_FIGURE_PROXIMITY:] + after[:_FIGURE_PROXIMITY])
    for metric in metrics:
        for word in _METRICS.get(metric, ()):
            if word in window:
                return True
    return False


def _stated_value(text: str) -> float | None:
    """The figure the claim itself asserts, normalised to absolute units."""
    match = _MONEY.search(text)
    if not match:
        return None
    try:
        value = float(match.group(1).replace(",", ""))
    except ValueError:
        return None
    unit = (match.group(2) or "").lower()
    if unit.startswith("b"):
        return value * 1e9
    if unit.startswith("m"):
        return value * 1e6
    return value


def substantiate(claim: Claim, history: dict[str, list[tuple[str, float]]]) -> Substantiation:
    """Test a claim against reported history.

    `history` maps a metric name to (period_label, value) pairs, oldest first.
    A record claim is checked against the comparison set its own wording implies:
    an all-time claim against every prior period, a "June quarter" claim only
    against the same fiscal quarter in other years.
    """
    if not claim.testable:
        return Substantiation(claim, Verdict.NOT_TESTABLE,
                              "Qualitative statement with no figure to test against.")

    if _found(claim.text, _GUIDANCE):
        return Substantiation(
            claim, Verdict.NOT_TESTABLE,
            "Forward-looking guidance. There is no filed figure for a period that "
            "has not happened yet.")

    scope = _scoping_term(claim.text)
    if scope:
        return Substantiation(
            claim, Verdict.NOT_TESTABLE,
            f"'{scope}' qualifies the figure, so it describes part of the business. "
            "The reported series covers the whole company; comparing the two would "
            "call a true statement false.")

    if _found(claim.text, _WINDOWED):
        return Substantiation(
            claim, Verdict.NOT_TESTABLE,
            "Measured over a rolling window that the quarterly reported series cannot express. "
            "Testing it against a single quarter would compare different things.")

    metric = next((m for m in claim.metrics if m in history), None)
    if metric is None:
        # No history is an absence of evidence, not evidence against. Operating
        # metrics like vehicle deliveries are not GAAP concepts and will never
        # appear in XBRL, so this must never read as a failed claim.
        return Substantiation(
            claim, Verdict.NOT_TESTABLE,
            f"No reported series available for {', '.join(_metric_label(m) for m in claim.metrics)} — "
            "this is an absence of data, not a contradiction of the claim.")

    series = history[metric]
    if len(series) < 2:
        return Substantiation(claim, Verdict.NOT_TESTABLE,
                              f"Only {len(series)} reported {metric} period(s) — too little history to test.")

    current_label, current_value = series[-1]
    prior = series[:-1]

    if claim.scope is Scope.SEASONAL:
        from ledger.statements import same_quarter
        prior = [(l, v) for l, v in prior if same_quarter(l, current_label)] or prior

    # A claim with no superlative asserts a figure, not a ranking. Check the
    # figure it states against the figure that was reported.
    if not claim.superlatives:
        # A sentence listing several figures is a set of contributing factors,
        # not one assertion about one metric. Picking the first number out of it
        # and calling it "the stated revenue" produces a confident false
        # accusation against a statement that claimed nothing of the kind.
        if not _figure_near_metric(claim.text, claim.metrics):
            return Substantiation(
                claim, Verdict.NOT_TESTABLE,
                "The figure is not attached to the metric — it sits elsewhere in a "
                "sentence that also mentions it, so binding the two would invent a "
                "claim the company did not make.")
        if len(_MONEY.findall(claim.text)) > 1:
            return Substantiation(
                claim, Verdict.NOT_TESTABLE,
                "States several figures in one sentence, so no single one can be "
                "attributed to the metric being checked.")
        stated = _stated_value(claim.text)
        if stated is None:
            return Substantiation(claim, Verdict.NOT_TESTABLE,
                                  "States no figure that can be matched against the reported series.")
        gap = abs(stated - current_value) / current_value if current_value else 1.0
        figures = {
            "stated in release": _fmt(stated),
            "reported to SEC": f"{current_label} = {_fmt(current_value)}",
            "difference": f"{gap:.2%}",
        }
        if gap <= 0.015:
            return Substantiation(claim, Verdict.SUPPORTED,
                                  f"The stated {_metric_label(metric)} matches the figure "
                                  f"reported for {current_label}.",
                                  figures)
        # A gap between a release figure and the reported series can be our
        # parsing, a different scope (a segment, an adjusted measure, a
        # year-to-date total), or a real mismatch — and nothing here can tell
        # those apart. Calling it UNSUPPORTED asserts the company said something
        # false. Numeric verdicts therefore go no further than "could not
        # reconcile"; UNSUPPORTED is reserved for record claims, where the same
        # series is compared against itself and the comparison is sound.
        return Substantiation(
            claim, Verdict.NOT_TESTABLE,
            f"Could not reconcile the stated {_metric_label(metric)} with the reported series for "
            f"{current_label}. Most often a different scope — a segment, an adjusted "
            "measure, or a year-to-date figure — rather than a false statement.",
            figures)

    if metric in _SPLIT_SENSITIVE:
        return Substantiation(
            claim, Verdict.NOT_TESTABLE,
            f"A record test on {metric} is unsafe: per-share figures are stored as filed and are "
            "not adjusted for stock splits, so historical values are not comparable.")

    # A record claim that also states a figure has to state the right one. This
    # path used to test the superlative alone, so a release reading "a record
    # $75.2 billion" was scored against the $81.61B actually filed and came back
    # QUALIFIED — the check confirmed a record while the number in the sentence
    # disagreed with the filing by billions, unremarked. As with the numeric
    # path, a gap is reported and never called false: it may be a segment, an
    # adjusted measure, or our own parsing.
    if len(_MONEY.findall(claim.text)) == 1:
        stated = _stated_value(claim.text)
        if stated is not None and current_value:
            gap = abs(stated - current_value) / current_value
            if gap > _RECORD_FIGURE_TOLERANCE:
                return Substantiation(
                    claim, Verdict.NOT_TESTABLE,
                    f"The release states {_fmt(stated)} where {current_label} reports "
                    f"{_fmt(current_value)}, a {gap:.0%} gap. The record cannot be tested "
                    "against a figure the release does not state — most often a segment, "
                    "an adjusted measure, or a different period.",
                    {"stated in release": _fmt(stated),
                     "reported to SEC": f"{current_label} = {_fmt(current_value)}",
                     "difference": f"{gap:.1%}"})

    best_label, best_value = max(prior, key=lambda p: p[1])
    figures = {
        "current": f"{current_label} = {_fmt(current_value)}",
        "prior best": f"{best_label} = {_fmt(best_value)}",
        "comparison set": f"{len(prior)} periods"
        + (" (same fiscal quarter only)" if claim.scope is Scope.SEASONAL else ""),
    }

    if current_value <= best_value:
        return Substantiation(claim, Verdict.UNSUPPORTED,
                              f"{best_label} reported higher {_metric_label(metric)} than the "
                              "period claimed as a record.",
                              figures)

    if claim.qualifiers:
        return Substantiation(claim, Verdict.QUALIFIED,
                              "Accurate as stated, but the company discloses a qualifier in the same release: "
                              + "; ".join(claim.qualifiers) + ".",
                              figures)

    if claim.scope is Scope.SEASONAL:
        overall_label, overall_value = max(series[:-1], key=lambda p: p[1])
        if overall_value > current_value:
            figures["higher period"] = f"{overall_label} = {_fmt(overall_value)}"
            return Substantiation(
                claim, Verdict.QUALIFIED,
                f"Holds only within the same fiscal quarter. {overall_label} reported "
                f"{overall_value / current_value - 1:+.0%} against the period claimed as a record — "
                "the quarter qualifier is carrying the claim.",
                figures)
        return Substantiation(claim, Verdict.QUALIFIED,
                              "A record only within the same fiscal quarter, not across all periods.",
                              figures)

    return Substantiation(claim, Verdict.SUPPORTED,
                          f"Highest {_metric_label(metric)} in the {len(prior)} prior "
                          "reported periods.", figures)

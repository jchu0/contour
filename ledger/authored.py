"""Per-company checks written by a model, executed deterministically.

The fixed checks ask the same nine questions of every registrant, and several
of them are the wrong questions: NHTSA holds no recalls for a chipmaker, and a
retailer's inventory tells you more than its recall history ever would.

So a model writes the checks. What it does *not* do is decide what is true. It
emits a specification — which reported concept, which filing section, which
phrase, which threshold, which direction — and the executor below runs that
specification against primary sources and computes every figure itself. The
model chooses where to look; the filing decides what is found.

Three consequences are load-bearing:

* A specification naming a concept the company never reported is reported
  UNAVAILABLE with that concept named. A model that invents `us-gaap:Vibes`
  produces a visible gap, never a silent pass.
* Every finding still cites the accession its figures came from, so a reader
  can re-derive the number without trusting the rule or its author.
* The rule's authorship travels with it to the page. A reader is told which
  checks a model wrote and why it thought they fit.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import date

MODEL = "claude-opus-5"
MAX_SPECS = 4                 # a scan is read on stage; four novel checks is plenty
MIN_THRESHOLD = 1.0           # below this every company flags on rounding
MAX_PHRASES = 6

KIND_TREND = "xbrl_trend"
KIND_RATIO = "xbrl_ratio"
KIND_TEXT = "text_watch"
KINDS = (KIND_TREND, KIND_RATIO, KIND_TEXT)

SECTIONS = {"1A": "Risk Factors", "7": "Management's Discussion and Analysis",
            "1": "Business", "3": "Legal Proceedings"}
DIRECTIONS = ("rise", "fall", "either")
MODES = ("appeared", "disappeared")
SEVERITIES = ("high", "medium", "low")

_KEY_OK = re.compile(r"^[a-z0-9_]{3,40}$")
_CONCEPT_OK = re.compile(r"^[A-Za-z][A-Za-z0-9]{2,79}(\|[A-Za-z][A-Za-z0-9]{2,79}){0,3}$")


class SpecError(ValueError):
    """A specification that cannot be trusted to run. Reported, never guessed at."""


@dataclass(frozen=True)
class CheckSpec:
    """One model-authored check. Declarative on purpose — there is no code here
    for a model to write, only parameters for the executor to read."""

    key: str
    title: str
    rationale: str            # why this check suits this company, in the model's words
    kind: str
    severity: str = "medium"
    # xbrl_trend / xbrl_ratio
    concept: str = ""
    denominator: str = ""
    direction: str = "either"
    threshold_pct: float = 10.0
    # text_watch
    section: str = "1A"
    phrases: tuple[str, ...] = ()
    mode: str = "appeared"
    # "catalogue" | "model" — a reader is told which, and the two are not the
    # same claim: one was written and reviewed, the other generated on request.
    origin: str = "model"

    @property
    def label(self) -> str:
        return self.title.strip() or self.key


def parse_spec(raw: dict) -> CheckSpec:
    """Validate one specification. Anything unrecognised is refused outright.

    The model is capable of returning a plausible-looking check that names a
    concept nobody reports or a section that does not exist. Refusing here is
    what keeps that from becoming a finding.
    """
    def text(name: str, limit: int) -> str:
        return str(raw.get(name, "") or "").strip()[:limit]

    kind = text("kind", 40)
    if kind not in KINDS:
        raise SpecError(f"unknown check kind {kind!r}")

    key = text("key", 40).lower().replace("-", "_").replace(" ", "_")
    if not _KEY_OK.match(key):
        raise SpecError(f"unusable check key {key!r}")

    title, rationale = text("title", 80), text("rationale", 400)
    if not title:
        raise SpecError("a check with no title cannot be shown to a reader")
    if not rationale:
        raise SpecError(f"{key}: no rationale given for why this check fits")

    severity = text("severity", 10).lower()
    if severity not in SEVERITIES:
        severity = "medium"
    origin = "catalogue" if text("origin", 20) == "catalogue" else "model"

    if kind == KIND_TEXT:
        section = text("section", 6).upper()
        if section not in SECTIONS:
            raise SpecError(f"{key}: section {section!r} is not one this report splits")
        phrases = tuple(
            p for p in (str(x).strip()[:80] for x in (raw.get("phrases") or []))
            if len(p) >= 3
        )[:MAX_PHRASES]
        if not phrases:
            raise SpecError(f"{key}: a text watch with no phrases would match nothing")
        mode = text("mode", 20).lower()
        if mode not in MODES:
            raise SpecError(f"{key}: unknown mode {mode!r}")
        return CheckSpec(key=key, title=title, rationale=rationale, kind=kind,
                         severity=severity, section=section, phrases=phrases,
                         mode=mode, origin=origin)

    concept = text("concept", 80)
    if not _CONCEPT_OK.match(concept):
        raise SpecError(f"{key}: {concept!r} is not a us-gaap concept name")
    denominator = text("denominator", 80)
    if kind == KIND_RATIO:
        if not _CONCEPT_OK.match(denominator):
            raise SpecError(f"{key}: ratio needs a denominator concept, got {denominator!r}")
    else:
        denominator = ""

    direction = text("direction", 20).lower()
    if direction not in DIRECTIONS:
        raise SpecError(f"{key}: unknown direction {direction!r}")
    try:
        threshold = float(raw.get("threshold_pct", 10.0))
    except (TypeError, ValueError):
        raise SpecError(f"{key}: threshold_pct is not a number") from None
    if not (MIN_THRESHOLD <= threshold <= 1000):
        raise SpecError(
            f"{key}: threshold {threshold}% is outside the usable range "
            f"({MIN_THRESHOLD}–1000%)"
        )

    return CheckSpec(key=key, title=title, rationale=rationale, kind=kind,
                     severity=severity, concept=concept, denominator=denominator,
                     direction=direction, threshold_pct=threshold, origin=origin)


def parse_specs(payload: list) -> tuple[list[CheckSpec], list[str]]:
    """(specs, rejections). Rejections are surfaced, not swallowed — a refused
    check is information about the author."""
    specs, problems, seen = [], [], set()
    for raw in payload or []:
        try:
            spec = parse_spec(raw if isinstance(raw, dict) else {})
        except SpecError as exc:
            problems.append(str(exc))
            continue
        if spec.key in seen:
            problems.append(f"{spec.key}: duplicate check key, kept the first")
            continue
        seen.add(spec.key)
        specs.append(spec)
        if len(specs) >= MAX_SPECS:
            break
    return specs, problems


# -- execution -------------------------------------------------------------

_ANNUAL_DAYS = range(330, 400)


# A concept whose newest annual period is older than this is not describing the
# company now. Microsoft stopped tagging `Revenues` in 2010; a check that reads
# it silently compares 2009 with 2010 and calls the result current.
STALE_AFTER_DAYS = 800


def annual_series(facts: dict, concept: str) -> list[tuple[str, float, str]]:
    """(period_end, value, filed) for a concept's annual periods, oldest first.

    `concept` may name alternates separated by "|". Filers retag over time —
    `Revenues` became `RevenueFromContractWithCustomerExcludingAssessedTax` for
    many registrants — so the alternate with the most recent data wins.

    Restatements report the same period more than once; the most recently filed
    value wins, matching how the rest of the ledger reads XBRL.
    """
    if "|" in concept:
        best: list[tuple[str, float, str]] = []
        for alternate in concept.split("|"):
            series = annual_series(facts, alternate.strip())
            if series and (not best or series[-1][0] > best[-1][0]):
                best = series
        return best

    entry = (facts.get("facts", {}).get("us-gaap", {}) or {}).get(concept)
    if not entry:
        return []
    latest: dict[str, tuple[float, str]] = {}
    for unit, rows in (entry.get("units") or {}).items():
        if unit not in ("USD", "shares", "USD/shares", "pure"):
            continue
        for row in rows:
            end, start, filed = row.get("end"), row.get("start"), row.get("filed", "")
            if not end or row.get("val") is None:
                continue
            if start:
                # A duration fact: keep only full years.
                span = (date.fromisoformat(end) - date.fromisoformat(start)).days
                if span not in _ANNUAL_DAYS:
                    continue
            elif row.get("form") != "10-K":
                # An instant — a balance-sheet item, which carries no span to
                # measure. Every quarter-end would otherwise qualify, and the
                # check would compare two consecutive quarters while calling
                # the result year over year. Only the value as reported in an
                # annual report is a fiscal year-end.
                continue
            if end not in latest or filed > latest[end][1]:
                latest[end] = (float(row["val"]), filed)
    return [(end, v, f) for end, (v, f) in sorted(latest.items())]


def pick_concept(facts: dict, concept: str) -> str:
    """Which alternate actually supplied the data.

    The spec may name three tags; exactly one of them produced the series, and
    that is the one a reader needs in order to re-derive the figure.
    """
    if "|" not in concept:
        return concept
    best, best_end = concept.split("|")[0], ""
    for alternate in concept.split("|"):
        series = annual_series(facts, alternate.strip())
        if series and series[-1][0] > best_end:
            best, best_end = alternate.strip(), series[-1][0]
    return best


def _stale(end: str) -> str | None:
    """A message when the newest annual period is too old to describe now.

    Reporting this as a gap rather than a clean result is the whole point: a
    comparison of two ancient periods looks exactly like a comparison of two
    current ones once the dates are off the screen.
    """
    try:
        age = (date.today() - date.fromisoformat(end)).days
    except ValueError:
        return None
    if age <= STALE_AFTER_DAYS:
        return None
    return ("{what} was last reported for the year ended " + end +
            f", {age // 365} years ago — the filer appears to have stopped using "
            "this tag, so a comparison would describe a period that is no longer current")


def _fmt(value: float) -> str:
    for scale, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(value) >= scale:
            return f"{value / scale:,.2f}{suffix}"
    return f"{value:,.2f}"


_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def humanise(concept: str) -> str:
    """"ResearchAndDevelopmentExpense" -> "Research and development expense".

    The exact tag stays in the evidence block, where a reader who wants to
    re-derive the figure can copy it. Prose gets the readable form.
    """
    words = _CAMEL.sub(" ", concept).split()
    if not words:
        return concept
    small = {"and", "or", "of", "the", "net", "in", "to", "for"}
    out = [words[0].capitalize()] + [
        w.lower() if w.lower() in small else w.lower() for w in words[1:]
    ]
    return " ".join(out)


def direction_phrase(direction: str, threshold: float) -> str:
    """A sentence fragment, not an enum. "flags a either beyond 8%" was the bug."""
    if direction == "rise":
        return f"a rise of more than {threshold:g}%"
    if direction == "fall":
        return f"a fall of more than {threshold:g}%"
    return f"a move of more than {threshold:g}% in either direction"


def _moved(direction: str, change_pct: float, threshold: float) -> bool:
    if direction == "rise":
        return change_pct >= threshold
    if direction == "fall":
        return change_pct <= -threshold
    return abs(change_pct) >= threshold


@dataclass
class SpecResult:
    """What running one spec produced. `unavailable` is a first-class outcome:
    a concept the company never reported is a gap, not a clean result."""

    findings: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    unavailable: str | None = None


def run_trend(spec: CheckSpec, facts: dict) -> SpecResult:
    concept = pick_concept(facts, spec.concept)
    series = annual_series(facts, spec.concept)
    if len(series) < 2:
        return SpecResult(unavailable=(
            f"{concept} is not reported with two comparable annual periods "
            f"in this company's XBRL facts — the rule named a concept the "
            f"registrant does not file"
        ))
    (prior_end, prior, _), (end, current, _) = series[-2], series[-1]
    if (stale := _stale(end)):
        return SpecResult(unavailable=stale.format(what=concept))
    if not prior:
        return SpecResult(unavailable=f"{concept} was zero in {prior_end}; no change to measure")
    change = (current - prior) / abs(prior) * 100
    result = SpecResult(notes=[
        f"{concept}: {_fmt(prior)} ({prior_end}) → {_fmt(current)} ({end})"
    ])
    if _moved(spec.direction, change, spec.threshold_pct):
        result.findings.append({
            "headline": (f"{humanise(concept)} "
                         f"{'rose' if change > 0 else 'fell'} {abs(change):.1f}% year over year"),
            "detail": (f"Reported {humanise(concept).lower()} moved from {_fmt(prior)} "
                       f"for the year ended {prior_end} to {_fmt(current)} for the year "
                       f"ended {end}. The rule flags "
                       f"{direction_phrase(spec.direction, spec.threshold_pct)}."),
            "evidence": {"concept": concept, "prior": _fmt(prior),
                         "current": _fmt(current), "change": f"{change:+.1f}%",
                         "threshold": f"{spec.threshold_pct:g}%"},
        })
    return result


def run_ratio(spec: CheckSpec, facts: dict) -> SpecResult:
    concept = pick_concept(facts, spec.concept)
    denominator = pick_concept(facts, spec.denominator)
    num, den = annual_series(facts, spec.concept), annual_series(facts, spec.denominator)
    missing = [c for c, s in ((concept, num), (denominator, den)) if len(s) < 2]
    if missing:
        return SpecResult(unavailable=(
            f"{' and '.join(missing)} not reported with two comparable annual "
            f"periods in this company's XBRL facts"
        ))
    den_by_end = {end: v for end, v, _ in den}
    pairs = [(end, v, den_by_end[end]) for end, v, _ in num if den_by_end.get(end)]
    if len(pairs) < 2:
        return SpecResult(unavailable=(
            f"{concept} and {denominator} share no two annual periods "
            f"with the same end date"
        ))
    (prior_end, pn, pd), (end, cn, cd) = pairs[-2], pairs[-1]
    if (stale := _stale(end)):
        return SpecResult(unavailable=stale.format(
            what=f"{concept} and {denominator}"))
    prior_ratio, current_ratio = pn / pd * 100, cn / cd * 100
    if not prior_ratio:
        return SpecResult(unavailable=f"the ratio was zero in {prior_end}; no change to measure")
    change = (current_ratio - prior_ratio) / abs(prior_ratio) * 100
    result = SpecResult(notes=[
        f"{concept}/{denominator}: {prior_ratio:.1f}% ({prior_end}) "
        f"→ {current_ratio:.1f}% ({end})"
    ])
    if _moved(spec.direction, change, spec.threshold_pct):
        result.findings.append({
            "headline": (f"{humanise(concept)} as a share of "
                         f"{humanise(denominator).lower()} "
                         f"{'rose' if change > 0 else 'fell'} to {current_ratio:.1f}%"),
            "detail": (f"{humanise(concept)} was {prior_ratio:.1f}% of "
                       f"{humanise(denominator).lower()} for the year ended "
                       f"{prior_end} and {current_ratio:.1f}% for the year ended {end}, "
                       f"a {abs(change):.1f}% move in the ratio. The rule flags "
                       f"{direction_phrase(spec.direction, spec.threshold_pct)}."),
            "evidence": {"concept": concept, "denominator": denominator,
                         "prior": f"{prior_ratio:.1f}%", "current": f"{current_ratio:.1f}%",
                         "change": f"{change:+.1f}%", "threshold": f"{spec.threshold_pct:g}%"},
        })
    return result


def run_text(spec: CheckSpec, current_text: str, prior_text: str) -> SpecResult:
    """A phrase entering or leaving a section between two annual filings.

    Both sides must be present. Comparing against a section that failed to
    resolve would report every phrase as newly appeared, which is the confident
    wrong answer this design exists to avoid.
    """
    if not current_text or not prior_text:
        which = "current" if not current_text else "prior"
        return SpecResult(unavailable=(
            f"Item {spec.section} ({SECTIONS[spec.section]}) did not resolve in the "
            f"{which} filing, so an appearance cannot be told from a parsing failure"
        ))
    now, before = current_text.lower(), prior_text.lower()
    result = SpecResult(notes=[
        f"watching {len(spec.phrases)} phrase(s) in Item {spec.section} "
        f"({SECTIONS[spec.section]})"
    ])
    for phrase in spec.phrases:
        needle = phrase.lower()
        in_now, in_before = needle in now, needle in before
        hit = (spec.mode == "appeared" and in_now and not in_before) or \
              (spec.mode == "disappeared" and in_before and not in_now)
        if not hit:
            continue
        haystack = now if in_now else before
        at = haystack.find(needle)
        source_text = current_text if in_now else prior_text
        start, end = max(0, at - 220), min(len(source_text), at + len(needle) + 320)
        result.findings.append({
            "headline": f'"{phrase}" {spec.mode} in Item {spec.section}',
            "detail": (f'The phrase "{phrase}" is '
                       f'{"present in the latest filing and absent from the prior one"
                          if spec.mode == "appeared" else
                          "absent from the latest filing and present in the prior one"}.'),
            "evidence": {"phrase": phrase, "section": f"Item {spec.section}",
                         "change": spec.mode},
            "passage": source_text[start:end].strip(),
            "from_prior": not in_now,
        })
    return result


# -- authoring -------------------------------------------------------------

MAX_CONCEPTS_OFFERED = 220

SYSTEM = """You write checks for an automated review of a public company's SEC filings.

You do not decide what is true. You emit specifications; a deterministic executor \
runs them against the filings and computes every figure. Your job is to choose \
*where to look* for this particular company.

You are given the concepts this registrant actually reports. Use only those \
names. A concept not on the list does not exist for this company, and a check \
naming one is discarded.

What makes a good check here:

1. It is specific to this company's business. A chip designer competes on R&D \
intensity; a retailer carries inventory risk; a bank carries credit loss \
provisioning. Do not write checks that would suit any company equally.
2. It would not already be covered. The fixed checks already cover: revenue \
composition and concentration, restatements of previously reported figures, \
year-over-year risk-factor changes, and management claims in earnings releases. \
Do not duplicate them.
3. Its threshold is one a reasonable analyst would defend, and it would not fire \
on ordinary movement. Prefer thresholds that would have been quiet in a normal \
year.
4. Its rationale says plainly why this company warrants this check. The rationale \
is shown to the reader beside the finding.

Write 2 to 4 checks. Fewer good checks beat more weak ones."""

TOOL = {
    "name": "emit_checks",
    "description": "Return the checks to run for this company.",
    "input_schema": {
        "type": "object",
        "properties": {
            "checks": {
                "type": "array",
                "minItems": 1,
                "maxItems": MAX_SPECS,
                "items": {
                    "type": "object",
                    "properties": {
                        "key": {"type": "string", "description": "lower_snake_case, 3-40 chars"},
                        "title": {"type": "string", "description": "short heading a reader sees"},
                        "rationale": {"type": "string",
                                      "description": "why THIS company warrants THIS check"},
                        "kind": {"type": "string", "enum": list(KINDS)},
                        "severity": {"type": "string", "enum": list(SEVERITIES)},
                        "concept": {"type": "string",
                                    "description": "us-gaap concept, from the offered list"},
                        "denominator": {"type": "string",
                                        "description": "xbrl_ratio only; from the offered list"},
                        "direction": {"type": "string", "enum": list(DIRECTIONS)},
                        "threshold_pct": {"type": "number",
                                          "description": f"{MIN_THRESHOLD}-1000"},
                        "section": {"type": "string", "enum": list(SECTIONS)},
                        "phrases": {"type": "array", "items": {"type": "string"},
                                    "maxItems": MAX_PHRASES},
                        "mode": {"type": "string", "enum": list(MODES)},
                    },
                    "required": ["key", "title", "rationale", "kind"],
                },
            }
        },
        "required": ["checks"],
    },
}


@dataclass(frozen=True)
class Authored:
    specs: tuple[CheckSpec, ...] = ()
    model: str | None = None
    written_on: str = ""
    available: bool = True
    reason: str | None = None
    rejected: tuple[str, ...] = ()


def available_concepts(facts: dict) -> list[str]:
    """Concepts with at least two comparable annual periods — the ones a check
    can actually be written against."""
    us_gaap = facts.get("facts", {}).get("us-gaap", {}) or {}
    out = []
    for concept in us_gaap:
        if len(annual_series(facts, concept)) >= 2:
            out.append(concept)
    return sorted(out)


def brief(ticker: str, company: str, profile: dict, concepts: list[str]) -> str:
    sic = profile.get("sic", "")
    label = profile.get("sic_description") or ""
    offered = concepts[:MAX_CONCEPTS_OFFERED]
    more = len(concepts) - len(offered)
    return (
        f"Company: {company} ({ticker.upper()})\n"
        f"Industry: {label} (SIC {sic})\n\n"
        f"Concepts this registrant reports with two or more comparable annual "
        f"periods ({len(concepts)} total"
        f"{f', {more} not listed' if more > 0 else ''}):\n"
        + ", ".join(offered)
        + "\n\nSections available for a text watch: "
        + ", ".join(f"Item {k} ({v})" for k, v in SECTIONS.items())
    )


def write_checks(ticker: str, company: str, profile: dict, facts: dict,
                 *, model: str = MODEL) -> Authored:
    """Ask the model for this company's checks. Never raises — a failure here
    leaves the fixed checks untouched and is reported like any other gap."""
    try:
        import anthropic
    except ImportError:
        return Authored(available=False,
                        reason="the anthropic package is not installed — run: pip install anthropic")

    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        return Authored(available=False, reason=(
            "no Anthropic credentials found. Set ANTHROPIC_API_KEY, or run `ant auth login`. "
            "The nine fixed checks below are computed without a model and are unaffected."
        ))

    concepts = available_concepts(facts)
    if len(concepts) < 5:
        return Authored(available=False, reason=(
            f"only {len(concepts)} reported concepts have two comparable annual "
            f"periods — too little history to write a check against"
        ))

    client = anthropic.Anthropic()
    try:
        response = client.messages.create(
            model=model,
            max_tokens=4000,
            system=SYSTEM,
            thinking={"type": "adaptive"},
            tools=[TOOL],
            messages=[{"role": "user",
                       "content": brief(ticker, company, profile, concepts)}],
        )
    except anthropic.NotFoundError as exc:
        return Authored(available=False, reason=f"model not found: {exc}")
    except anthropic.RateLimitError:
        return Authored(available=False, reason="rate limited — try again shortly")
    except anthropic.APIStatusError as exc:
        return Authored(available=False, reason=f"API error {exc.status_code}: {exc.message}")
    except anthropic.APIConnectionError:
        return Authored(available=False, reason="could not reach the Anthropic API")

    payload = None
    for block in response.content:
        if getattr(block, "type", "") == "tool_use" and block.name == TOOL["name"]:
            payload = (block.input or {}).get("checks")
            break
    if payload is None:                      # no tool call — accept JSON in the text
        text = "".join(b.text for b in response.content if getattr(b, "type", "") == "text")
        match = re.search(r'\{.*"checks".*\}', text, re.S)
        if match:
            try:
                payload = json.loads(match.group(0)).get("checks")
            except json.JSONDecodeError:
                payload = None
    if not payload:
        return Authored(available=False, reason="the model returned no usable checks")

    specs, problems = parse_specs(payload)

    # A concept that survived the syntax check can still be one this registrant
    # never files. Catching it here names the offending check instead of letting
    # it surface later as a mystery gap.
    known = set(concepts)
    kept, dropped = [], list(problems)
    for spec in specs:
        missing = [c for c in (spec.concept, spec.denominator)
                   if c and c not in known]
        if missing:
            dropped.append(f"{spec.key}: {', '.join(missing)} not reported by this company")
            continue
        kept.append(spec)

    if not kept:
        return Authored(available=False, model=model, rejected=tuple(dropped),
                        reason="every proposed check was refused; see the rejections")
    return Authored(specs=tuple(kept), model=model, written_on=date.today().isoformat(),
                    rejected=tuple(dropped))


# -- persistence -----------------------------------------------------------

from pathlib import Path

from ledger import profile  # noqa: E402 — kept beside the code that uses it

AUTHORED_DIR = profile.config_dir() / "authored"


def _path(ticker: str, directory: Path = AUTHORED_DIR) -> Path:
    return directory / f"{ticker.strip().upper()}.json"


def save(ticker: str, authored: Authored, directory: Path = AUTHORED_DIR) -> None:
    """Written once, at track time, and reused by every later scan.

    A check whose definition drifted between scans would make the delta between
    them meaningless, so the specification is pinned until a person regenerates it.
    """
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "ticker": ticker.strip().upper(),
        "model": authored.model,
        "written_on": authored.written_on,
        "rejected": list(authored.rejected),
        "checks": [
            {k: (list(v) if isinstance(v, tuple) else v)
             for k, v in spec.__dict__.items()}
            for spec in authored.specs
        ],
    }
    _path(ticker, directory).write_text(
        json.dumps(payload, indent=2), encoding="utf-8"
    )


def load(ticker: str, directory: Path = AUTHORED_DIR) -> Authored:
    """Whatever was pinned for this ticker. A missing file is not an error —
    most companies simply have no authored checks yet."""
    path = _path(ticker, directory)
    if not path.exists():
        return Authored(available=False, reason="no checks have been written for this company yet")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return Authored(available=False, reason=f"{path}: {exc}")
    specs, problems = parse_specs(data.get("checks") or [])
    if not specs:
        return Authored(available=False, model=data.get("model"),
                        rejected=tuple(problems),
                        reason="the pinned checks no longer validate")
    return Authored(specs=tuple(specs), model=data.get("model"),
                    written_on=str(data.get("written_on") or ""),
                    rejected=tuple(data.get("rejected") or []) + tuple(problems))


def forget(ticker: str, directory: Path = AUTHORED_DIR) -> bool:
    path = _path(ticker, directory)
    if path.exists():
        path.unlink()
        return True
    return False


# -- entity resolution -----------------------------------------------------

ENTITY_SYSTEM = """You map a public company to the name a given data source files it under.

Each source is searched by NAME, not by ticker or CIK. The registrant's legal \
name on EDGAR is frequently not the name a source uses: a registrant may file as \
"ALPHABET INC" while a docket names "Google", or as "Rivian Automotive, Inc. / DE" \
while a news source says "Rivian".

Return the search term most likely to retrieve THIS company and not another. \
Rules:

1. Prefer the shortest form that is still unambiguous. "Apple" is ambiguous in a \
news source and fine in a federal docket; say so in your reasoning.
2. If a shorter form would collide with an unrelated company, use the longer one.
3. Set confidence honestly. Low confidence is useful; a confident wrong name \
attributes another company's regulatory or safety record to this one.
4. If you cannot map the company for a source, omit it. Omitting is correct \
behaviour, not failure."""

ENTITY_TOOL = {
    "name": "emit_mappings",
    "description": "Return the search name for each source you can map.",
    "input_schema": {
        "type": "object",
        "properties": {
            "mappings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "source": {"type": "string", "description": "the source name given"},
                        "entity": {"type": "string", "description": "the search term to use"},
                        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                        "reasoning": {"type": "string",
                                      "description": "why this term, and what it might collide with"},
                    },
                    "required": ["source", "entity", "confidence", "reasoning"],
                },
            }
        },
        "required": ["mappings"],
    },
}


@dataclass(frozen=True)
class Mapping:
    source: str
    entity: str
    confidence: str
    reasoning: str
    model: str


@dataclass(frozen=True)
class Resolved:
    mappings: tuple[Mapping, ...] = ()
    available: bool = True
    reason: str | None = None


def resolve_entities(ticker: str, company: str, sources: list[str],
                     *, model: str = MODEL) -> Resolved:
    """Propose the name each source should be searched by. Never raises."""
    if not sources:
        return Resolved(available=False, reason="no sources need a name mapping")
    try:
        import anthropic
    except ImportError:
        return Resolved(available=False, reason="the anthropic package is not installed")
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        return Resolved(available=False, reason=(
            "no Anthropic credentials found. Set ANTHROPIC_API_KEY, or map the name "
            "by hand on the Data sources page."
        ))

    client = anthropic.Anthropic()
    prompt = (f"Registrant: {company}\nTicker: {ticker.upper()}\n\n"
              f"Sources needing a name:\n"
              + "\n".join(f"- {s}" for s in sources))
    try:
        response = client.messages.create(
            model=model, max_tokens=2000, system=ENTITY_SYSTEM,
            thinking={"type": "adaptive"}, tools=[ENTITY_TOOL],
            messages=[{"role": "user", "content": prompt}],
        )
    except anthropic.APIStatusError as exc:
        return Resolved(available=False, reason=f"API error {exc.status_code}: {exc.message}")
    except anthropic.APIConnectionError:
        return Resolved(available=False, reason="could not reach the Anthropic API")

    payload = None
    for block in response.content:
        if getattr(block, "type", "") == "tool_use" and block.name == ENTITY_TOOL["name"]:
            payload = (block.input or {}).get("mappings")
            break
    if not payload:
        return Resolved(available=False, reason="the model proposed no mappings")

    wanted = {s.lower(): s for s in sources}
    out = []
    for raw in payload:
        if not isinstance(raw, dict):
            continue
        name = wanted.get(str(raw.get("source", "")).strip().lower())
        entity = str(raw.get("entity", "") or "").strip()[:120]
        if not name or not entity:
            continue
        conf = str(raw.get("confidence", "")).lower()
        out.append(Mapping(
            source=name, entity=entity,
            confidence=conf if conf in ("high", "medium", "low") else "low",
            reasoning=str(raw.get("reasoning", "") or "").strip()[:300],
            model=model,
        ))
    if not out:
        return Resolved(available=False, reason="no proposal matched a source that needs one")
    return Resolved(mappings=tuple(out))

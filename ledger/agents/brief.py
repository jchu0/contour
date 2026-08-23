"""The analyst brief.

The executive summary narrates what the checks found. This goes a step further
and is the second place a model is allowed to sit: it reads the computed
findings *and* the declared-source hits together and says how the reported
performance holds up — which evidence supports it, which cuts against it, and
what could not be checked at all.

Three rules shape everything here:

1. It never gives advice. No buy, hold, sell, valuation, price target, or
   "investors should". It summarises evidence on both sides and stops.
2. It never introduces a figure. Every number must already appear in the
   material it is handed, which is itself computed from filings.
3. Class B-F material corroborates and never proves. A news story cannot
   promote a claim to fact; it can only agree or disagree with one, and the
   brief has to say which it is doing.

Without credentials the brief falls back to a cached one if the company has
been briefed before, labelled with when it was written. A cached brief is
never presented as fresh.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from datetime import date, datetime

from ledger import profile
from ledger.agents import MODEL, call, credentials
from ledger.report import Report, Status

MAX_TOKENS = 4000

SYSTEM = """You are a filings analyst. You are handed everything an automated \
review computed about one public company from its SEC filings, plus every hit \
from declared corroborating sources. You write a brief on how the company's \
reported performance holds up under that evidence.

WHAT YOU ARE FOR

Piece the material together. A single finding is rarely the story; the story is \
usually how several of them relate. If revenue held up because of a component \
that is shrinking, say so. If earnings lean on something that is not the \
operating business, say so. If a risk factor was removed in the same year the \
thing it described got worse, that is worth putting side by side.

Then weigh it. For each thread, give the evidence that supports the company's \
reported picture and the evidence that cuts against it. Both. A thread with \
only one side is either a thread you have not finished or one you should drop.

HARD RULES

1. NEVER give advice. No buy, sell, hold, valuation, price target, "investors \
should", "worth watching", or any steer about what to do. You describe \
evidence. Someone else decides.
2. NEVER introduce a number, date, or fact that is not in the material you were \
given. You have no other knowledge of this company. If you want a figure that \
is not there, write around it.
3. NEVER assert wrongdoing or intent. "Revenue fell while credits fell faster" \
is a fact. "Tesla propped up revenue with credits" is an accusation. Write the \
first.
4. Weigh material by its class, which is stated on every line. Class A and B \
are primary: a regulator's notice or a company's own release is authoritative \
for what it states on its face, though only a figure computed from a filing is \
a finding. Class C through F is CORROBORATION ONLY — it can agree or disagree \
with a filing, never establish a fact on its own. Say which class you are \
leaning on. A community post is the weakest thing on the page.
5. Say what could not be checked. A reader who is not told about a gap will \
assume there was none. A check that DOES NOT APPLY is not a gap.
6. Attribute every claim to the check or source it came from, so a reader can \
go and look.

OUTPUT

Return JSON only, no prose around it, matching:

{
  "headline": "one sentence on what the evidence shows, no advice",
  "threads": [
    {
      "title": "short label, e.g. Revenue quality",
      "reading": "2-4 sentences putting the material together",
      "supporting": ["evidence that supports the reported picture, each with its source"],
      "against": ["evidence that cuts against it, each with its source"],
      "sources": [{"label": "what it is", "url": "...", "klass": "A"}]
    }
  ],
  "corroboration": "one or two sentences on what the B-F material adds and what it cannot settle",
  "not_checked": ["what could not be checked, and why"]
}

Two to four threads. Keep every string tight — this is read in a minute."""

DIGEST_SYSTEM = """You are handed one-line summaries of several companies that \
were reviewed today. Write a short daily digest across all of them.

Same rules as a single brief: no advice, no figures that are not in the \
material, no assertions of wrongdoing, and say plainly where the evidence is \
thin. Lead with whatever moved most. Name companies by ticker.

Return JSON only:

{
  "headline": "one sentence across the whole watchlist",
  "lines": [{"ticker": "TSLA", "note": "one sentence"}],
  "gaps": ["anything that could not be checked across the set"]
}"""


@dataclass(frozen=True)
class Brief:
    """A written brief, or a stated reason there is none."""

    headline: str = ""
    threads: list = field(default_factory=list)
    corroboration: str = ""
    not_checked: list = field(default_factory=list)
    model: str | None = None
    written: str | None = None
    cached: bool = False
    material: str = ""
    provenance: str = ""
    available: bool = True
    reason: str | None = None


def _briefs_dir():
    return profile.config_dir() / "briefs"


def brief_input(report: Report) -> str:
    """Everything the model may see: computed findings, then corroboration.

    The two are separated on purpose. A model handed one undifferentiated pile
    will cite a forum post the way it cites a 10-K.
    """
    lines = [f"COMPANY: {report.company} ({report.ticker}), CIK {report.cik}"]
    if report.filing:
        lines.append(f"MOST RECENT ANNUAL REPORT: {report.filing.form} filed {report.filing.filed}")

    lines += ["", "=== COMPUTED FROM FILINGS (CLASS A — primary) ==="]
    for check in report.checks:
        if check.key.startswith("custom_"):
            continue
        if check.status == Status.UNAVAILABLE:
            lines.append(f"[{check.title}] COULD NOT RUN — {check.reason}")
            continue
        if check.status == Status.NOT_APPLICABLE:
            lines.append(f"[{check.title}] DOES NOT APPLY — {check.reason}")
            continue
        if check.status == Status.CLEAN:
            lines.append(f"[{check.title}] checked, nothing crossed a threshold")
            continue
        lines.append(f"[{check.title}]")
        for item in check.items:
            lines.append(f"  - ({item.severity}) {item.headline}")
            if item.detail:
                lines.append(f"    {item.detail[:300]}")
            if item.quote:
                lines.append(f'    quoted: "{item.quote[:220]}" — {item.attribution}')
            if item.evidence:
                lines.append("    figures: " + ", ".join(f"{k}={v}" for k, v in item.evidence.items()))
            if item.source and item.source.url:
                lines.append(f"    source: class {item.source.klass.letter} · "
                             f"{item.source.origin} · {item.source.document_date} · {item.source.url}")
        for note in check.notes:
            lines.append(f"  note: {note}")

    # Declared sources are grouped by what they actually are, not by the fact
    # that they were declared. The Federal Register is a regulator: printing it
    # under a "CLASS B-F, corroboration only" header contradicted its own class
    # tag on the very next line, and the reader had to pick which to believe.
    primary, corroborating = [], []
    for check in report.checks:
        if not check.key.startswith("custom_") or not check.items:
            continue
        for item in check.items:
            source = item.source
            klass = source.klass.letter if source else "?"
            (primary if klass in ("A", "B") else corroborating).append(
                (check.title, klass, source, item))

    def render(rows: list) -> list[str]:
        out, seen = [], None
        for title, klass, source, item in rows:
            if title != seen:
                out.append(f"[{title}]")
                seen = title
            when = source.document_date if source else "?"
            out.append(f"  - (class {klass}, {when}) {item.headline}")
            if item.detail:
                out.append(f"    {item.detail[:200]}")
            if source and source.url:
                out.append(f"    {source.url}")
        return out

    lines += ["", "=== DECLARED SOURCES, PRIMARY (CLASS A-B — authoritative on "
              "what they state, but not computed from a filing) ==="]
    lines += render(primary) or ["(none)"]
    lines += ["", "=== CORROBORATION ONLY (CLASS C-F — cannot establish a fact) ==="]
    lines += render(corroborating) or ["(none)"]
    if not primary and not corroborating:
        lines.append("(no declared source returned anything for this company)")
    return "\n".join(lines)


def _parse(payload: str, *, model: str, written: str,
           cached: bool = False, provenance: str = "") -> Brief:
    data = json.loads(payload)
    return Brief(
        headline=str(data.get("headline", "")).strip(),
        threads=list(data.get("threads") or []),
        corroboration=str(data.get("corroboration", "")).strip(),
        not_checked=list(data.get("not_checked") or []),
        model=model, written=written, cached=cached, provenance=provenance,
    )


def load_cached(ticker: str) -> Brief | None:
    path = _briefs_dir() / f"{ticker.upper()}.json"
    if not path.exists():
        return None
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    brief = _parse(json.dumps(stored.get("brief", {})),
                   model=stored.get("model", "unknown"),
                   written=stored.get("written", "unknown"),
                   cached=True,
                   provenance=stored.get("provenance", ""))
    return replace(brief, material=str(stored.get("material", "")))


def material_fingerprint(text: str) -> str:
    """What the brief was written from, in twelve characters.

    A brief describes one scan. If the material is identical the brief would
    say the same thing, so there is nothing to pay for a second time.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def save_brief(ticker: str, brief: Brief, *, provenance: str = "") -> None:
    path = _briefs_dir() / f"{ticker.upper()}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "ticker": ticker.upper(),
        "model": brief.model,
        "written": brief.written or date.today().isoformat(),
        "provenance": provenance or brief.provenance,
        "material": brief.material,
        "brief": {
            "headline": brief.headline,
            "threads": brief.threads,
            "corroboration": brief.corroboration,
            "not_checked": brief.not_checked,
        },
    }, indent=2) + "\n", encoding="utf-8")


def analyst_brief(report: Report, *, model: str = MODEL, use_cache: bool = True) -> Brief:
    """Write the brief. A failure is reported, never raised — the report stands.

    A stored brief is reused whenever the material behind it has not changed —
    with or without credentials. The cache used to be consulted only when there
    was no key, so every view of a report was a fresh billed call and the prose
    drifted between two readings of the same scan.
    """
    material = brief_input(report)
    fingerprint = material_fingerprint(material)
    cached = load_cached(report.ticker) if use_cache else None
    if cached and cached.material == fingerprint:
        return cached
    if credentials() is not None:
        if cached:
            # Stale, but written from a real scan of this company. Better than
            # nothing, and it says on the page when it was written.
            return cached
        return Brief(available=False, reason=(
            "no Anthropic credentials, and this company has not been briefed before. "
            "Every finding on this page was computed without a model and stands on "
            "its own; the brief only reads them together."))
    text, reason = call(SYSTEM, material, model=model, max_tokens=MAX_TOKENS)
    if reason:
        return Brief(available=False, reason=reason)
    try:
        brief = _parse(text, model=model,
                       written=datetime.now().date().isoformat(), provenance="live")
    except ValueError:
        return Brief(available=False, reason="the model did not return usable JSON")
    brief = replace(brief, material=fingerprint)
    save_brief(report.ticker, brief, provenance="live")
    return brief


def load_digest() -> dict | None:
    path = _briefs_dir() / "digest.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
